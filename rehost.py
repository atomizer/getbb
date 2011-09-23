#!/usr/bin/python
# 2010 atomizer
# version 0.6

from __future__ import print_function

import sys
import os
import re
import urllib2
import string
import random
import argparse
from urllib2 import build_opener, install_opener, urlopen, URLError
from urlparse import urlparse
from tempfile import TemporaryFile

# from http://atlee.ca/software/poster/
from encode import multipart_encode, MultipartParam, gen_boundary
from streaminghttp import streaming_opener

__all__ = ['rehost', 'open_thing', 'print_urlerror', 'DOWNLOAD_URL']

USER_AGENT = ''
UPLOAD_URL = 'http://file.kirovnet.ru/upload'
DOWNLOAD_URL = r'http://file.kirovnet.ru/d/\d+'
MAX_SIZE = 50 * 2 ** 20
TIMEOUT = 60

ERR = '[!]'
FLAGS = '(?si)'

RW = (
    (r'radikal\.ru/\w/(.+)\.html?$', r'\1', 0),
    # http://fastpic.ru/view/7/2010/0616/5439056de5527a6dc085ff9ffd186715.jpg.html
    # http://i7.fastpic.ru/big/2010/0616/15/5439056de5527a6dc085ff9ffd186715.jpg
    (   r'fastpic\.ru/view/(\d+)/(\d+)/(\d+)/([^\.]+?)(\w\w)\.([^\.]+)\.html?$',
        r'i\1.fastpic.ru/big/\2/\3/\5/\4\5.\6', 0),
    # http://www.bitbest.ru/view.php?img=2010_10_20_1254978563.jpg
    # http://www.bitbest.ru/files/2010_10_20_1254978563.jpg
    (r'bitbest\.ru/view\.php\?.*?img=([^&]+).*', r'bitbest.ru/i/\1', 0),
    # http://img.phyrefile.com/hdlover/2009/12/09/7_002.png
    # http://pic.phyrefile.com/h/hd/hdlover/2009/12/09/7_002.png
    (   r'img\.phyrefile\.com/((\w)(\w)\w*)/(.*)',
        r'pic.phyrefile.com/\2/\2\3/\1/\4', 0),
    (r'.*?(http://(?:www\.)?ag\.ru/screenshots/\w+/\d+).*', r'\1/big', 1),
    # http://iceimg.com/0997a2a5e67e61.png.htm
    # http://iceimg.com/i/09/97/a2a5e67e61.png
    (r'(iceimg\.com)/(\w\w)(\w\w)(.+)\.htm$', r'\1/i/\2/\3/\4', 0),
    # http://picsee.net/2011-06-02/9d0c7fbe483f.jpg.html
    # http://picsee.net/upload/2011-06-02/9d0c7fbe483f.jpg
    (r'(picsee\.net)/(.+)\.html$', r'\1/upload/\2', 0),
)
RW_EXT = (
    ('phyrefile\.com/image/view', 'id="main_content".*?href="([^"]+)'),
    ('bak\.lan/pictures/share', '<input.*?class="code_box".*?value="([^"]+)'),
    ('ipicture\.ru/Gallery/Viewfull/', '<input.*?type="text".*?value="([^"]+)'),
    ('epikz\.net/s/', r'href="(http://epikz\.net/i/[^"]+)'),
    ('10pix\.ru/view/', r'src="([^"]+10pix\.ru/img[^"]+)'),
    ('imageshack\.us/(?:i/|photo/)', 'rel="image_src" href="([^"]+)'),
    ('imageban\.ru/show/', r'id=imagecode.*?<img[^>]+src="([^"]+)'),
    ('lostpic\.net/\?', r'([^"]+?lostpic\.net/images/[^"]+)'),
    ('ag\.ru/screenshots/', r'href="([^"]+?screenshots\.ag\.ru/[^"]+)'),
    ('xmages\.net/show', r'src="([^"]+?xmages\.net/storage/[^"]+)'),
    ('directupload\.net/file', r'src="([^"]+?directupload.net/images/[^"]+)'),
)

IMAGE_TYPES = ('image/jpeg', 'image/jpg', 'image/gif', 'image/x-ms-bmp', 'image/png')
IMAGE_EXT = ('.jpg', '.jpg', '.gif', '.bmp', '.png')

cache_cfg = {}
cache_cfg['enabled'] = True
try:
    cache_cfg['file'] = os.path.join(os.path.dirname(__file__), 'linkcache.txt')
    with open(cache_cfg['file'], 'a+'):
        pass
except:
    cache_cfg['enabled'] = False


def print_urlerror(url, ex):
    msg = str(getattr(ex, 'code', ''))
    if msg: msg = 'HTTP ' + msg
    msg += str(getattr(ex, 'reason', ''))
    if not msg: msg = str(ex)
    print(ERR, 'Request to', url, 'failed:', msg)


def uaopener(handler=urllib2.BaseHandler, uagent=USER_AGENT):
    """Build an opener with spoofed user-agent."""
    op = build_opener(handler)
    op.addheaders = [('User-Agent', uagent)]
    return op


# Every urlopen() will use our special opener instead of default one.
install_opener(uaopener())


def cache_search(address):
    """Find out if object at this address is already rehosted."""
    if not cache_cfg['enabled']:
        return None
    with open(cache_cfg['file'], 'a+') as f:
        for cs in f:
            try:
                sl, fl = cs.strip().split()[:2]
                if sl == address:
                    return fl
            except ValueError:
                pass  # bad format


def cache_write(src, dl):
    """Remember the download URL for re-use."""
    if not cache_cfg['enabled']:
        return None
    if src != dl and not cache_search(src):
        with open(cache_cfg['file'], 'a+') as cf:
            cf.write('{0}\t{1}\n'.format(src, dl))


def open_thing(address, accept_types=None):
    """Try to open an URL or local file.
    
    Return a tuple (file, type, info), where:
    -- file: file object or None if an error occured
    -- type: MIME type (if known)
    -- info: httplib.HTTPMessage object (if present)
    """
    f, t, i = None, None, None
    pa = urlparse(address)
    if pa.scheme in ['http', 'https', 'ftp']:
        try:
            tmp = urlopen(address, timeout=TIMEOUT)
        except URLError as ex:
            print_urlerror(address, ex)
            return (f, t, i)
        except:
            print(ERR, 'Bad URL:', address)
            return (f, t, i)
        i = tmp.info()
        i.url = tmp.url
        t = i.gettype()
        if accept_types is not None and t not in accept_types:
            return (f, t, i)
        try:
            f = TemporaryFile()
            f.write(tmp.read())
            f.flush()
            f.seek(0)
        except Exception as ex:
            print(ERR, ex)
            return (None, None, None)
    else:
        # Unknown protocol, suppose it's local file path.
        fp = os.path.normpath(address)
        if os.path.isfile(fp):
            try:
                f = open(fp, 'rb')
            except IOError as ex:
                print(ERR, 'I/O error.', ex)
        else:
            print(ERR, 'Unknown object:', fp)
    return (f, t, i)


def recover_image(url):
    """Apply URL-rewriting rules in effort to get direct link."""
    for (L, R, C) in RW:
        dlink = re.sub(L, R, url)
        if dlink != url:
            if C == 0:
                return dlink
            else:  # continue rewriting
                url = dlink
                break
    try:
        page = urlopen(url)
    except URLError as ex:
        print_urlerror(url, ex)
        return url
    except:
        return url
    for (L, R) in RW_EXT:
        if re.search(L, page.url) is None:
            continue
        try:
            return re.search(FLAGS + R, page.read()).group(1)
        except (AttributeError, IndexError, URLError):
            print(ERR, 'Failed to get direct URL:', url)
            return url
    return url


def rehost(url, force_cache=False, image=False, referer=''):
    """Take URL or file path, return download URL.
    
    If image=True, also try to retrieve direct link before rehosting.
    """
    if re.match(DOWNLOAD_URL, url):
        return url  # already there
    cl = cache_search(url)
    if cl is not None:
        return cl  # already in cache
    
    if image:
        s = recover_image(url)
        ts = IMAGE_TYPES
    else:
        s = url
        ts = None
    
    if referer:
        op = uaopener()
        op.addheaders += [('Referer', referer)]
        install_opener(op)
    
    fd, ftype, finfo = open_thing(s, accept_types=ts)
    if fd is None:
        return url  # failed to open or wrong type
    if finfo is not None:
        fname = ''.join(random.sample(string.lowercase, 6))
        e = re.search(r'\.\w+$', finfo.url)
        if e is None:
            e = ''
        else:
            e = e.group()
        if image and ftype is not None:
            e = IMAGE_EXT[IMAGE_TYPES.index(ftype)]
        fname += e
    else:
        fname = fd.name
    pf = MultipartParam('file', filetype=ftype, fileobj=fd, filename=fname)
    if pf.get_size(gen_boundary()) > MAX_SIZE:
        print(ERR, 'Too big object:', s)
        return url
    datagen, headers = multipart_encode([pf])
    req = urllib2.Request(UPLOAD_URL, datagen, headers)
    try:
        pd = streaming_opener().open(req, timeout=TIMEOUT)
        page = pd.read().decode(pd.info().getparam('charset'))
    except URLError as ex:
        print_urlerror(UPLOAD_URL, ex)
        return url
    
    g = re.search(FLAGS + DOWNLOAD_URL, page)
    if g:
        g = g.group(0)
    else:
        g = re.search(FLAGS + '<div id="error">(.*?)</div>', page)
        if g:
            g = re.sub('<[^>]+>', '', g.group(1)).strip()
            print(ERR, 'file.kirovnet.ru says:', g)
        else:
            print(ERR, 'Failed to get URL (layout changed?)')
        return url    # falling back
    
    if force_cache or finfo is not None:
        cache_write(url, g)
    return g


if __name__ == '__main__':
    p = argparse.ArgumentParser(
        description='Automatic uploader to file.kirovnet.ru',
        epilog='Latest version and more info at https://github.com/atomizer/getbb'
    )
    p.add_argument(
        'targets', metavar='target', nargs='+',
        help='local file or URL to be uploaded'
    )
    p.add_argument(
        '-o', dest='output', default=sys.stdout, type=argparse.FileType('w'),
        help='write output to file (default: stdout)'
    )
    p.add_argument(
        '-i', '--image', action='store_true',
        help='treat targets as images (try to get direct URL)'
    )
    p.add_argument(
        '-nc', '--no-cache', dest='use_cache', action='store_false',
        help='don\'t use URL cache at all'
    )
    p.add_argument(
        '-fc', '--force-cache', action='store_true',
        help='force caching (default: cache only non-local)'
    )
    a = p.parse_args()
    cache_cfg['enabled'] = cache_cfg['enabled'] and a.use_cache
    t = []
    for arg in a.targets:
        t += [rehost(arg, image=a.image, force_cache=a.force_cache)]
    print('\n'.join(t), file=a.output)
    if a.output == sys.stdout and os.name == 'nt':
        sys.stdin.readline()
