#!/usr/bin/python
# 2010 atomizer
# version 0.9.pre
"""
Get BBcode source from compiled HTML page.

Seems to work with:
    rutracker.org
    epidemz.net
    hdclub.org
    pirat.ca
    
Usage:
    $ python getbb.py url [output_file]
    $ python getbb.py page.htm [output_file]
"""

from __future__ import print_function

import sys
import os
import re
from urlparse import urlparse, urlunparse
from hashlib import sha1

try:
    import gevent
    from gevent import monkey; monkey.patch_socket()
    from gevent.pool import Pool
except ImportError:
    gevent = None
    
from rehost import rehost, print_urlerror, uaopener

FLAGS = '(?si)'
# self-closed tags
ntag_re = re.compile(FLAGS +
    '<(?P<tag>[^/ >]+)(?P<attr>[^>]*)(?P<content>)/>')
# paired tags
ptag_re = re.compile(FLAGS +
    '<(?P<tag>[^/ >]+)(?P<attr>[^>]*)>(?P<content>[^<]*)</[^>]+>')

PSTO_PATTERNS = (
    '<div class="post_body"[^>]*>(.*?)</div><!--/post_body',  # rutracker-alike
    'class="heading_b"[^>]*>(.*?)</table>',  # hdclub
    '<div id="news-id-[^>]*>(.*?)</td>',  # epidemz
)
SIMPLE_RULES = (
    ('\n',''), ('\r',''), ('<wbr>',''), ('<!--.*?-->',''),
    # line breaks, horisontal rulers
    ('<span class="post-br">.*?</span>','\n\n'),
    ('<span class="post-hr">.*?</span>','[hr]'),
    ('<hr[^>]*>','[hr]'),
    ('<br[^>]*>','\n'),
    ('<div></div>','\n'),
    # lists
    ('<[ou]l>','[list]'), ('</[ou]l>','[/list]'),
    ('<[ou]l type="([^"])">','[list=\\1]'),
    ('<li>','[*]'),  ('</li>',''),
    # ?
    ('<b>','[b]'), ('</b>','[/b]'),
    ('<i>','[i]'), ('</i>','[/i]'),
    ('<u>','[u]'), ('</u>','[/u]'),
)
COMPLEX_RULES = {
    'post-b': ('[b]','[/b]'),
    'post-i': ('[i]','[/i]'),
    'post-u': ('[u]','[/u]'),
    'font-weight: ?bold': ('[b]','[/b]'),
    'font-style: ?italic': ('[i]','[/i]'),
    'text-decoration: ?underline': ('[u]','[/u]'),
    'href="([^"]+)': ('[url=_]','[/url]'),
    'src="([^"]+)': ('[img]','[/img]'),
    'class="postImg" title="([^"]+)': ('[img]','[/img]'),
    'class="postImg [^"]*?img-([^ "]*)[^>]*?title="([^"]+)': ('[img=_]','[/img]'),
    'text-align: ?([^;"]+)': ('[align=_]', '[/align]'),
    '[^v]align="([^"]+)': ('[align=_]', '[/align]'), # hdclub, epidemz
    'color: ?([^;"]+)': ('[color=_]', '[/color]'),
    'font-size: ?(\d+)': ('[size=_]',  '[/size]'),
    'font-family: ?([^;"]+)': ('[font="_"]','[/font]'),
    'spoiler-wrap': ('{#SP#}','[/spoiler]'), # hdclub, pirat.ca, epidemz(?)
    'sp-wrap': ('{#SP#}','[/spoiler]'), # rutracker
    '(spoiler-head|sp-head)': ('{#SHS#}','{#SHE#}'),
    'sp-body[^>]* title="([^"]+)': ('{#SHS#}_{#SHE#}',''),
    'class="q"': ('[quote]','[/quote]'),
    'class="quote"': ('[quote]','[/quote]'),
    'class="q" head="([^"]+)': ('[quote="_"]','[/quote]'),
    'c-body': ('[code]','[/code]'),
    'post-pre': ('[font="monospace"]','[/font]'),
    'float: ?(left|right)': ('{#FLOAT#}',''),
}

BANNED_TAGS = ('fieldset', 'table', 'style', 'form',)
SKIP_TAGS = ('object', 'param', 'embed', 'script', 'p',)
SKIP_TAGS_ATTR = (
    'display: none', '"heading"', 'colhead',
    'sp-fold', 'q-head', 'c-head', 'sp-title',
)
TAGS_WITH_URLS = ('a', 'var', 'img',)
# no nesting allowed ([b][b]test[/b][/b] -> [b]test[/b])
BBTAGS_NO_NEST = (
    '[b]', '[i]', '[u]', '[color=_]', '[align=_]', '[size=_]',
)

POOL_SIZE = 5

site_root = ''
target_root = ''
output_file = 'out.txt'
urls = {}

def fail(s):
    print('[!]', s)
    # sys.stdin.readline()
    sys.exit()
    
    
def decode_html_entities(string):
    # http://snippets.dzone.com/posts/show/4569
    from htmlentitydefs import name2codepoint as n2cp
    def substitute_entity(match):
        ent = match.group(3)
        if match.group(1) == "#":
            if match.group(2) == '':
                return unichr(int(ent))
            elif match.group(2) == 'x':
                return unichr(int('0x'+ent, 16))
        else:
            cp = n2cp.get(ent)
            if cp: return unichr(cp)
            else: return match.group()
    entity_re = re.compile(r'&(#?)(x?)(\w+);')
    return entity_re.sub(substitute_entity, string)
    
    
def hashurl(u):
    return sha1(u).hexdigest()
    
    
def reduce_nest(code, left, right, srcleft, srcright):
    L = '{#L#}'; R = '{#R#}'
    # Hide braces in replacements
    opt = left.replace('[', L).replace(']', R)
    clt = right.replace('[', L).replace(']', R)
    # Escape braces in patterns
    sl = srcleft.replace('[', r'\[').replace(']', r'\]')
    sr = srcright.replace('[', r'\[').replace(']', r'\]')
    sl = sl.replace('_', r'[^\]]+')
    # Magic!
    code = re.sub(FLAGS + r'((\[[^/\]]+\])*' + sl + ')', clt + r'\1', code)
    code = re.sub(FLAGS + '(' + sr + r'(\[/[^\]]+\])*)', r'\1' + opt, code)
    # Revert braces
    code = code.replace(L, '[').replace(R, ']')
    # Empty pairs removal
    r = (left + code + right).replace(left + right, '')
    return r
    
def proctag(m):
    """Return a replacement for single HTML tag."""
    global urls
    d = m.groupdict()
    d['tag'] = d['tag'].lower()
    if d['tag'] in SKIP_TAGS:
        return ''
    for t in SKIP_TAGS_ATTR:
        if re.search(t, d['attr']):
            return ''
            
    dc = d['content']
    for (i,v) in COMPLEX_RULES.iteritems():
        dm = re.search(FLAGS + i, d['attr'])
        if dm is None:
            continue
        optag = v[0]
        cltag = v[1]
        try:
            g = dm.groups('')[0]
        except IndexError:
            g = ''
        # Fix hdclub fucked-up colors.
        if site_root.find('hdclub') > 0 and cltag == '[/color]':
            if g == '#999966': g = '#005000'
            if g == '#006699': g = '#000000'
        # <div style="float:right"><img/></div> => [img=right]
        if optag == '{#FLOAT#}':
            dc = re.sub(r'\[img\]', '[img=' + g + ']', dc)
            optag = ''
        
        optag = optag.replace('_', g)
        if v[0] == '[img=_]': g = dm.groups('')[1]
        
        if d['tag'] in TAGS_WITH_URLS and g != '':
            if urlparse(g).scheme == '':
                if g[0] == '/':
                    g = site_root + g
                else:
                    g = target_root + g
            if urlparse(g).scheme == 'http':
                g_ = hashurl(g)  # something unique?
                # Save url and it's replacement for future.
                urls[g_], g = g, g_
                if d['tag'] == 'a':
                    optag = v[0].replace('_', g)
                if d['tag'] in ('var', 'img'):
                    dc = g
            else:
                # Omit tags with weird URLs.
                return dc
        # Spoilers
        if optag == '{#SP#}':
            hs = re.search('{#SHS#}(.*?){#SHE#}', dc)
            if hs:
                print(repr(hs.group(1)))
                optag = u'[spoiler="{0}"]'.format(
                    re.sub(r'\[[^\]]+\]', '', hs.group(1)))
                dc = dc.replace(hs.group(0),'')
            else:
                optag = '[spoiler]'
        # [pre] emulation (via &npsp;)
        if d['tag'] == 'pre':
            dc = dc.replace(' ', '&#160;')
        
        if v[0] in BBTAGS_NO_NEST:
            return reduce_nest(dc, optag, cltag, v[0], v[1])
        return optag + dc + cltag
    # unknown tags
    return d['content']
    
    
def process(s):
    global urls
    urls = {}
    # Reduce the page.
    for p in PSTO_PATTERNS:
        m = re.search(FLAGS + p, s)
        if m:
            s = m.group(1)
    # Cut out bad tags.
    for t in BANNED_TAGS:
        s = s.split('<' + t)[0]
    # Apply simple rules.
    for (k, r) in SIMPLE_RULES:
        s = re.sub(FLAGS + k, r, s)
    # Apply complex rules.
    (s, n) = ntag_re.subn(proctag, s)
    m, n = n, 1
    while n > 0:
        (s,n) = ptag_re.subn(proctag, s)
        m += n
    # Strip out any HTML leftovers.
    s = re.sub('<[^>]+>','',s)
    
    def print_urls(a, b):
        if a != b:
            print('{0} >> {1}'.format(a, b))
    
    # Rehost images.
    if gevent:
        pool = Pool(POOL_SIZE)
        def finale(url):
            def f(g):
                urls[hashurl(url)] = g.value
                print_urls(url, g.value)
            return f
        for url in urls.itervalues():
            j = pool.spawn(rehost, url, image=True)
            j.link_value(finale(url))
        pool.join()
    else:
        for pat, url in urls.iteritems():
            new_url = rehost(url, image=True)
            urls[pat] = new_url
            print_urls(url, new_url)
            
    # Bring URLs back in places.
    imgs = 0
    for p, url in urls.iteritems():
        if hashurl(url) != p:
            imgs += 1
        s = s.replace(p, urls[p])
    
    print('\nReplaced {0} tags, {1} images.'.format(m, imgs))
    return decode_html_entities(s).strip()


if __name__ == '__main__':
    if not sys.argv[1:]:
        print(__doc__)
        sys.exit()
    if sys.argv[2:]:
        output_file = sys.argv[2]
    
    target = sys.argv[1]
    tp = urlparse(target)
    site_root = urlunparse([tp.scheme, tp.netloc, '', '', '', '',])
    target_root = site_root + re.sub('[^/]$', '', tp.path)
    target_charset = 'cp1251'
    try:
        inpf = uaopener().open(target)
        c = inpf.info().getparam('charset')
        if c: target_charset = c
    except (IOError, ValueError):
        try:
            inpf = open(target)
        except IOError as ex:
            fail('I/O error at \'{0}\': {1}'.format(target, ex))
        site_root = ''
        target_root = ''
    
    istr = unicode(inpf.read(), target_charset)
    try:
        istr = process(istr)
    except KeyboardInterrupt as ex:
        fail(str(ex))
        
    f = open(output_file, 'w')
    f.write(istr.encode('utf-8'))
    
    print('Output written to \'{0}\''.format(output_file))
    if os.name == 'nt': os.startfile(output_file, 'open')
    