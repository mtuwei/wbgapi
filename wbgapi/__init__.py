
'''wbgapi provides a comprehensive interface to the World Bank's data and
metadata API with built-in pandas integration
'''

import urllib.parse
from functools import reduce
import requests
from tabulate import tabulate
from . import series
from . import source
from . import economy
from . import time
from . import region
from . import income
from . import lending
from . import data
try:
    import pandas as pd
except ImportError:
    pd = None


# defaults: these can be changed at runtime with reasonable results
endpoint = 'https://api.worldbank.org/v2'
lang = 'en'
per_page = 1000          # you can increase this if you start getting 'service unavailable' messages, which can mean you're sending too many requests per minute
db = 2
proxies = None           # see https://requests.readthedocs.io/en/master/user/advanced/#proxies for format

# The maximum URL length is 1500 chars before it reports a server error. Internally we use a smaller
# number for head room as well as to provide for the query string
api_maxlen = 1400

class APIError(Exception):
  def __init__(self,url,msg,code=None):
    self.msg  = msg
    self.url  = url
    self.code = code

  def __str__(self):
    if self.code:
        return 'APIError: [{}] {} ({})'.format(self.code, self.msg, self.url)

    return 'APIError: {} ({})'.format(self.msg, self.url)

class URLError(Exception):
    pass

class Metadata():
    def __init__(self,concept,id):
        self.concept = concept
        self.id = id
        self.metadata = {}

    def __repr__(self):

        def segment(d):
            return '\n--------\n'.join(['{}: {}'.format(k, v) for k,v in d.items()]) + '\n'
            
        s = '========\n{}: {}\n\n'.format(self.concept, self.id) +  segment(self.metadata)

        subsets = {'series': 'Economy-Series', 'economies': 'Series-Economy', 'time': 'Series-Time'}
        for k,v in subsets.items():
            if hasattr(self, k):
                d = getattr(self, k)
                if len(d):
                    s += '========\n{}\n\n'.format(v) + segment(d)

        return s

    def _repr_html_(self):

        def segment(concept, meta, id=None):
            if id:
                s = '<h4>{}: {}</h4>'.format(concept, id)
            else:
                s = '<h5>{}</h5>'.format(concept)

            rows = []
            for k,v in meta.items():
                rows.append([k, v])

            # here we don't call htmlTable because we wrap the entire output in a <div/>
            return s + tabulate(rows, tablefmt='html', headers=['field', 'value'])

        s = '<div class="wbgapi">' + segment(self.concept, self.metadata, id=self.id)
        subsets = {'series': 'Economy-Series', 'economies': 'Series-Economy', 'time': 'Series-Time'}
        for k,v in subsets.items():
            if hasattr(self, k):
                d = getattr(self, k)
                if len(d):
                    s += segment(v, d)

        return s + '</div>'


class Featureset():
    def __init__(self, items, columns=None):
        ''' can be initialized with any iterable
        '''

        self.items = list(items)
        self.columns = columns if columns else ['id', 'value']

    def __repr__(self):

        rows = self.table()
        if len(rows) == 0:
            return ''

        return tabulate(rows, tablefmt='simple', headers=self.columns)

    def _repr_html_(self):

        rows = self.table()
        if len(rows) == 0:
            return ''

        return htmlTable(rows, headers=self.columns)

    def table(self):

        rows = []
        if len(self.items) == 0:
            return rows

        for row in self.items:
            rows.append([row[k] for k in self.columns])

        rows.append(['', '{} elements'.format(len(rows))])
        return rows

class Coder(dict):
    '''Class returned by coder if passed a list of terms
    '''

    def __repr__(self):
        rows = self._coder_report()
        columns = rows.pop(0)
        return tabulate(rows, tablefmt='simple', headers=columns)

    def _repr_html_(self):
        rows = self._coder_report()
        columns = rows.pop(0)
        return htmlTable(rows, headers=columns)

    def _coder_report(self):
        return economy.coder_report(self)


def fetch(url, params={}, concepts=False, lang=None):
    '''Iterate over an API request with automatic paging.  The API returns a
    variety of response structures depending on the endpoint. fetch() sniffs
    the response structure and return the most appropriate set of iterated objects.

    Arguments:
        url:        partial URL (minus the base URL and langage) for the API query, minus the query string

        params:     optional query string parameters (required defaults are supplied by the function)

        concepts:   pass True to return results at the concept level, as opposed to the element/variable level

        lang:       preferred language. Pass none to use the global default

    Returns:
        a generator object.

    Example:
        for row in wbgapi.data.fetch('countries'):
          print(row['id'], row['name'])

    Notes:
        For most use cases there are higher level functions that are easier and safer than
        calling fetch() directly. But it's still very useful for direct testing and discovery
        of the API.
    '''

    global endpoint, per_page

    params_ = {'per_page': per_page}
    params_.update(params)
    params_['page'] = 1
    params_['format'] = 'json'

    totalRecords = None
    if lang is None:
       lang = globals()['lang']

    recordsRead = 0
    while totalRecords is None or recordsRead < totalRecords:

        url_ = '{}/{}/{}?{}'.format(endpoint, lang, url, urllib.parse.urlencode(params_))
        (hdr,result) = _queryAPI(url_)

        if totalRecords is None:
            totalRecords = int(hdr['total'])

        data = _responseObjects(url_, result, wantConcepts=concepts)
        for elem in data:
            yield elem

        recordsRead += int(hdr['per_page'])
        params_['page'] += 1

def refetch(url, variables, **kwargs):
    ''' repeating fetch: provides a variation of fetch() that allows URLs that exceed the maximium API limit to
    be chunked.

    Arguments:
        url:            partial URL with tokens, each of which must have values specified as function arguments. See example below

        variables:      array of variables to be chunked if necessary, in the order they should be chunked

        **kwargs:       remaining arguments MUST include values for each token in the url string. All arguments to fetch
                        are also acceptable and are passed to fetch. Any "chunkable" variable should be a semicolon-separated
                        value array. Arrays and iterables are not acceptable values

    Returns:
        A generator object

    Example:
        # fetch all indicators for Brazil and Argentina
        s = ';'.join([row['id'] for row in wbgapi.series.list()])
        for row in wbgapi.data.refetch('sources/{source}/series/{series}/country/{economy}', ['series', 'economy'], source=2, series=s, economy='BRA;ARG'):
            print(row)
    '''

    concepts = kwargs.get('concepts', False)
    lang     = kwargs.get('lang', None)
    params   = kwargs.get('params', {})

    try:
        for url2 in _refetch_url(url, variables[0], variables[1:], **kwargs):
            for row in fetch(url2, params, concepts, lang):
                yield row
    except URLError:
        raise ValueError('{}: parameters exceed the API\'s maximum limit'.format(url))

def get(url, params={}, concepts=False, lang=None):
    '''Return a single response from the API

    Arguments:
        url:        full URL for the API query, minus the query string

        params:     optional query string parameters (required defaults are supplied by the function)

        concepts:   pass True to return a result at the concept level, as opposed to the element/variable level

    Returns:
        First row from the response

    Example:
        print(wbgapi.get('countries/BRA')['name'])
    '''

    global endpoint

    if lang is None:
       lang = globals()['lang']

    params_ = params.copy()
    params_['page'] = 1
    params_['format'] = 'json'
    params_['per_page'] = 1

    url_ = '{}/{}/{}?{}'.format(endpoint, lang, url, urllib.parse.urlencode(params_))
    (hdr,result) = _queryAPI(url_)
    data = _responseObjects(url_, result, wantConcepts=concepts)
    return data[0] if len(data) > 0 else None


def metadata(url, variables, concepts='all', **kwargs):
    '''Return metadata records

    Arguments:
        url:        url with tokens, as per refetch()

        variables:  variables that can be chunked, in priority order (see refetch)

        concepts:   Name or list-like of the concepts to return: 'all' for all concepts

        **kwargs:   Remaining arguments to pass to refetch (must include varables for tokens in url)

    Returns:
        a generator that returns Metadata objects

    Example:
        for meta in wbgapi.metadata('sources/2/series/{series}/country/{economy}/metadata', ['series', 'economy'],
            concepts=['Series','Country-Series'], series='SP.POP.TOTL', economy='FRA;CAN'):
                print(meta.concept, meta.id, len(meta.metadata))
                
    Notes:
        Each return from the generator will include a unique concept/id pair and a complete corresponding metadata record
    '''

    if concepts == 'all':
        concepts = None
    elif type(concepts) is str:
        concepts = [concepts]

    def metafield(concept):
        '''Subroutine for returning individual metadata elements
        '''

        for var in concept['variable']:
            id = var['id']
            for field in var['metatype']:
                yield (concept['id'], var['id'], field)
    
    m = Metadata(None,None)
    for row in refetch(url, variables, concepts=True, **kwargs):
        if concepts and row['id'] not in concepts:
            continue

        for (concept_name,variable_id,field) in metafield(row):
            if concept_name != m.concept or variable_id != m.id:
                if m.concept:
                    yield m

                m = Metadata(concept_name, variable_id)
            
            m.metadata[field['id']] = field['value']

    if m.concept:
        yield m

def search(q, concepts='all', db=None):
    ''' search database metadata for matching text.

    Arguments:
        url:        url with tokens, as per refetch()

        concepts:   Name or list-like of the concepts to return: 'all' for all concepts

        **kwargs:   Remaining arguments to pass to refetch (must include varables for tokens in url)

    Returns:
        a generator that provides Metadata objects (same as metadata())

    Notes:
        The return of this function is the same as for the metadata() function. The difference
        is that the metadata property contains matching metadata fields and values.

    Examples:
        for row in wbgapi.search('fossil fuels'):
            print(row)

    '''
    
    if db is None:
        db = globals()['db']

    for row in metadata('sources/{source}/search/{q}', ['source'], concepts=concepts, source=str(db), q=urllib.parse.quote(q, safe='')):
        yield row

def _responseHeader(url, result):
    '''Internal function to return the response header, which contains page information
    '''

    if type(result) is list and len(result) > 0 and type(result[0]) is dict:
        # looks like the v2 data API
        return result[0]

    if type(result) is dict:
        # looks like the new beta advanced API
        return result

    raise APIError(url, 'Unrecognized response object format')

def _responseObjects(url, result, wantConcepts=False):
    '''Internal function that returns an array of objects
    '''

    if type(result) is list and len(result) > 1:
        # looks like the v2 data API
        return result[1]

    if type(result) is dict and result.get('source'):
        if type(result['source']) is list and len(result['source']) > 0 and type(result['source'][0]) is dict:
            # this format is used for metadata and concept lists. Caller may need an array of concepts or
            # an array of the variables of the first concept
            if wantConcepts:
                return result['source'][0]['concept']
            else:
                return result['source'][0]['concept'][0]['variable']

        if type(result['source']) is dict:
            # this format is used to return data in the beta endpoints
            return result['source']['data']

    raise APIError(url, 'Unrecognized response object format')

def _queryAPI(url):
    '''Internal function for calling the API with sanity checks
    '''
    global proxies

    response = requests.get(url, proxies=proxies)
    if response.status_code != 200:
        raise APIError(url, response.reason, response.status_code)

    try:
        result = response.json()
    except:
        raise APIError(url, 'JSON decoding error')

    hdr = _responseHeader(url, result)
    if hdr.get('message'):
        msg = hdr['message'][0]
        raise APIError(url, '{}: {}'.format(msg['key'], msg['value']))

    return (hdr, result)

_latest_concept_cache = {}

def queryParam(arg, concept=None, db=None):
    ''' Prepare parameters for an API query. This is a core function
    called by several dimension-specific functions of the same name

    Arguments:
        arg:        a record identifier or list-like of identifiers

        concept:    concept for the arguments passed

        db:         database; pass None to access the global database

    Returns:
        a semicolon separated API-ready parameter string
    '''

    if db is None:
        db = globals()['db']

    if type(arg) is str and arg == 'mrv' and concept:
        global _latest_concept_cache

        if _latest_concept_cache.get(db) is None:
            _latest_concept_cache[db] = {}

        if _latest_concept_cache[db].get(concept) is None:
            for row in source.features(concept):
                _latest_concept_cache[db][concept] = row['id']

        arg = _latest_concept_cache[db].get(concept, '')
        
    if type(arg) is str or type(arg) is int:
        arg = [arg]

    if concept == 'time':
        v = time.periods(db)
        return ';'.join(map(lambda x: str(v.get(str(x),x)), arg))

    # this will throw an exception if arg is not iterable, which is what we want it to do
    return ';'.join(map(lambda x:str(x), arg))

def pandasSeries(data, key='id',value='value',name=None):
    '''Convert an object array to a pandas Series object. This core function is
    called by several dimension-specific implementation functions

    Arguments:
        data:       an object array. Each object becomes a Series row

        key:        field for the Series index

        value:      field for the Series column values

        name:       Series column name. If None, same as value

    Returns:
        a pandas Series object
    '''

    
    if pd is None:
        raise ModuleNotFoundError('you must install pandas to use this feature')

    if name is None:
        name = value

    return pd.Series({row[key]: row[value] for row in data}, name=name)

def htmlTable(*args, **kwargs):
    '''Generates an HTML table wrapped in a <div class="wbgapi"/> to allow users
       to customize the display if they wish. All arguments are passed to tabulate;
       you should not include the 'tablefmt=html' parameter
    '''

    return '<div class="wbgapi">' + tabulate(*args, tablefmt='html', **kwargs) + '</div>'

def _refetch_url(url, var, variables, **kwargs):
    '''Used to chunk potentially very long URLs smaller ones by splitting long arguments

    Returns a generator of URLs that will not exceed the API's maximum string length
    '''

    global api_maxlen

    def subdivide(parts):
        # split a long semicolon separated string into 2 roughly equal segments, on a semicolon boundary

        parts2 = []
        for s in parts:
            mp = int(len(s)/2)
            of = s[mp:].find(';')
            if of < 0:
                # part can't be subdivided
                parts2.append(s)
            else:
                parts2.extend([s[:mp+of], s[mp+of+1:]])
        
        return parts2

    kw = kwargs.copy()

    # parts is an array of roughly equal chunks of the value for var. We first try to see
    # if the entire string can be passed as one, else we chunk it into smaller and smaller pieces
    parts = [kwargs[var]]
    sz = None
    while True:
        kw[var] = max(parts, key=len)
        test_url = url.format(**kw)
        if len(test_url) < api_maxlen:
            # we are small enough
            for elem in parts:
                kw[var] = elem
                yield url.format(**kw)

            return

        # else, subdivide parts and try again
        parts2 = subdivide(parts)
        if len(parts) == len(parts2):
            # can't subdivide any more
            break
        else:
            parts = parts2

    # by now we've chunked as much as we can on var. If that's not enough, then we
    # start chunking the next variable
    if len(variables) == 0:
        # if there's no more variables then we cry Uncle and give up
        raise URLError()

    for elem in parts:
        kw[var] = elem
        for u2 in _refetch_url(url, variables[0], variables[1:], **kw):
            yield u2
