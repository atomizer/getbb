#!/usr/bin/python
# 2010 atomizer
# version 0.5.pre
"""
Automatic uploader to file.kirovnet.ru.

Usage:
    $ python rehost.py [files] [URLs] ...
"""

from __future__ import print_function

import sys
import os
import re
import urllib2
from urllib2 import build_opener, install_opener, urlopen, URLError
from mimetypes import guess_type
from urlparse import urlparse
from tempfile import TemporaryFile

# from http://atlee.ca/software/poster/
from encode import multipart_encode, MultipartParam, gen_boundary
from streaminghttp import streaming_opener

__all__ = ['rehost', 'uaopener', 'print_urlerror']

USER_AGENT = 'Mozilla/5.0 Firefox/3.6.12'
UPLOAD_URL = 'http://file.kirovnet.ru/upload'
DOWNLOAD_URL = r'http://file.kirovnet.ru/d/\d+'
MAX_SIZE = 50 * 2 ** 20
TIMEOUT = 60
CACHE_FILE = os.path.join(os.path.dirname(__file__), 'linkcache.txt')

ERR = '[!]'
FLAGS = '(?si)'

RW = (
    (r'radikal.ru/\w/(.+)\.html?$', r'\1'),
    (r'fastpic.ru/view/([^/]+)/([^/]+)/([^/]+)/([^\.]+?)(\w\w).([^\.]+).html?$',
        r'i\1.fastpic.ru/big/\2/\3/\5/\4\5.\6'),
    (r'bitbest.ru/view.php\?.*?img=([^&]+).*', r'bitbest.ru/files/\1'),
)
RW_EXT = (
    ('phyrefile.com/image/view', 'id="main_content".*?href="([^"]+)'),
    ('bak.lan/pictures/share', '<input.*?class="code_box".*?value="([^"]+)'),
    ('ipicture.ru/Gallery/Viewfull/', '<input.*?type="text".*?value="([^"]+)'),
    ('img.epidemz.net/s/', '<input.*?type="text".*?value="([^"]+)'),
    ('10pix.ru/view/', 'src="([^"]+10pix.ru/img[^"]+)'),
    ('imageshack.us/i/', 'rel="image_src" href="([^"]+)'), #???
)

def print_urlerror(url, ex):
    msg = str(getattr(ex, 'code', ''))
    if msg: msg = 'HTTP ' + msg
    msg += str(getattr(ex, 'reason', ''))
    if not msg: msg = str(ex)
    print(ERR, 'Request to \'{0}\' failed: {1}'.format(url, msg))
    
    
def uaopener(handler=urllib2.BaseHandler, uagent=USER_AGENT):
    """Build an opener with spoofed user-agent."""
    op = build_opener(handler)
    op.addheaders = [('User-Agent', uagent)]
    return op
    
    
# Every urlopen() will use our special opener instead of default one.
install_opener(uaopener())

def cache_search(address):
    """Find out if object at this address is already rehosted."""
    for cs in open(CACHE_FILE, 'a+').readlines():
        sl, fl = cs.strip().split()
        if sl == address: return fl
    
    
def cache_write(src, dl):
    """Remember the download URL for re-use."""
    if src != dl and not cache_search(src):
        open(CACHE_FILE, 'a+').write('{0}\t{1}\n'.format(src, dl))
    
    
def open_thing(address):
    """Try to open an URL or local file.
    
    Return a tuple (file, type), where:
    -- file: file object or None if an error occured
    -- type: guessed MIME type
    """    
    f, t = None, None
    pa = urlparse(address)
    if pa.scheme in ['http', 'https', 'ftp', 'file']:
        try:
            f = urlopen(address, timeout=TIMEOUT)
        except URLError as ex:
            print_urlerror(address, ex)
            return (None, None)
        except ValueError:
            print(ERR, 'Bad URL: \'{0}\''.format(address))
            return (None, None)
        t = f.info().type
        s = re.search(r'\.[^/]+$', pa.path)
        if s is None: s = ''
        else: s = s.group()
        tmp = TemporaryFile(prefix='_', suffix=s)
        tmp.write(f.read())
        tmp.flush()
        f = tmp
    else:
        # Unknown protocol, suppose it's local file path.
        fp = os.path.normpath(address)
        if os.path.isfile(fp):
            t = guess_type(fp)
            try:
                f = open(fp, 'rb')
            except IOError as ex:
                print(ERR, 'I/O error.', ex)
        else:
            print(ERR, 'Unrecognized object: \'{0}\''.format(fp))
    
    if t is None or t == '':
        t = 'application/octet-stream'
    return (f, t)
    

def recover_image(url):
    """Apply URL-rewriting rules in effort to get direct link."""
    for (L, R) in RW_EXT:
        if re.search(L, url) is None:
            continue
        try:
            page = urlopen(url).read()
        except URLError as ex:
            print_urlerror(url, ex)
            return url
        try:
            return re.search(FLAGS + R, page).group(1)
        except IndexError:
            print(ERR, 'Layout changed at \'', urlparse(url).netloc,
                '\' - unable to parse!')
            return url
    for (L, R) in RW:
        dlink = re.sub(L, R, url)
        if dlink != url:
            return dlink
    return url
    
    
def rehost(url, cache=True, image=False):
    """Take URL or file path, return download URL.
    
    If image=True, also try to retrieve direct link before rehosting.
    
    Usage:
        rehost('http://my.host.org/song.mp3')
        rehost('/home/me/doc.rst')
    """
    
    cl = cache_search(url)
    if cl is not None:
        return cl  # already in cache
    
    if image:
        s = recover_image(url)
    else:
        s = url
    
    fd, ftype = open_thing(s)
    if fd is None:
        return s  # failed to open
    if image and ftype.find('image') != 0:
        print('Not an image, ignored:', s)
        return s
    
    pf = MultipartParam('file', filetype=ftype, fileobj=fd, filename=fd.name)
    if pf.get_size(gen_boundary()) > MAX_SIZE:
        print(ERR, 'Too big object: \'{0}\''.format(s))
        return s
    datagen, headers = multipart_encode([pf])
    
    req = urllib2.Request(UPLOAD_URL, datagen, headers)
    try:
        page = streaming_opener().open(req, timeout=TIMEOUT).read()
    except URLError as ex:
        print_urlerror(UPLOAD_URL, ex)
        return s
        
    try:
        g = re.search(FLAGS + DOWNLOAD_URL, page).group()
    except:
        print(ERR, 'Uploaded, but failed to get URL - layout changed?')
        return s    # falling back
    
    if cache:
        cache_write(url, g)
    return g
    
if __name__ == '__main__':
    # TODO: show GUI here
    
    if sys.argv[1:]:
        for arg in sys.argv[1:]:
            t = rehost(arg)
            if arg != t: print(t)  # something better would be cool.
        sys.stdin.readline()
    else:
        print(__doc__)
