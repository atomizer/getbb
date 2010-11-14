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

try:
    import Image
except ImportError:
    pass

import rehost as rehost_m
from rehost import *

FLAGS = '(?si)'
# self-closed tags
ntag_re = re.compile(FLAGS +
    '<(?P<tag>[^/ >]+)(?P<attr>[^>]*)(?P<content>)/>')
# paired tags
ptag_re = re.compile(FLAGS +
    '<(?P<tag>[^/ >]+)(?P<attr>[^>]*)>(?P<content>[^<]*)</[^>]+>')

PSTO_PATTERNS = (
    'class="post_body"[^>]*>(.*?)</div><!--/post_body',  # rutracker-alike
    'class="heading_b"[^>]*>(.*?)<a name="startcomments"',  # hdclub
    'id="news-id-[^>]*>(.*?)<br>',  # epidemz
)
SIMPLE_RULES = (
    ('\n', ''), ('\r', ''), ('<wbr>', ''), ('<!--.*?-->', ''),
    # line breaks, horisontal rulers
    ('<span class="post-br">.*?</span>', '\n\n'),
    ('<span class="post-hr">.*?</span>', '[hr]'),
    ('<hr[^>]*>', '[hr]'),
    ('<br[^>]*>', '\n'),
    ('<div></div>', '\n'),
    # lists
    ('<[ou]l>', '[list]'), ('</[ou]l>', '[/list]'),
    ('<[ou]l type="([^"])">', '[list=\\1]'),
    ('<li>', '[*]'),  ('</li>', ''),
    # hdclub & epidemz dumb tags
    ('<b>', '[b]'), ('</b>', '[/b]'),
    ('<i>', '[i]'), ('</i>', '[/i]'),
    ('<u>', '[u]'), ('</u>', '[/u]'),
    # hdclub's textarea
    ('<textarea>', '[font="monospace"]'),
    ('</textarea>', '[/font]'),
)
COMPLEX_RULES = (
    # simple text formatting
    ('post-b', ('[b]','[/b]')),
    ('post-i', ('[i]','[/i]')),
    ('post-u', ('[u]','[/u]')),
    ('font-weight: ?bold', ('[b]','[/b]')),
    ('font-style: ?italic', ('[i]','[/i]')),
    ('text-decoration: ?underline', ('[u]','[/u]')),
    ('color: ?([^;"]+)', ('[color=_]', '[/color]')),
    ('font-size: ?(\d+)', ('[size=_]',  '[/size]')),
    ('font-family: ?([^;"]+)', ('[font="_"]','[/font]')),
    # URLs
    ('href="([^"]+)', ('[url=_]','[/url]')),
    # images
    ('src="([^"]+)', ('[img]','[/img]')),
    ('class="postImg" title="([^"]+)', ('[img]','[/img]')),
    ('class="postImg [^"]*?img-([^ "]*)[^>]*?title="([^"]+)',
        ('[img=_]','[/img]')),
    # align
    ('text-align: ?([^;"]+)', ('[align=_]', '[/align]')),
    (' align="([^"]+)', ('[align=_]', '[/align]')), # hdclub, epidemz
    ('float: ?(left|right)', ('{#FLOAT#}','')),
    # spoilers
    ('spoiler-wrap', ('{#SP#}','[/spoiler]')), # hdclub, pirat.ca, epidemz(?)
    ('sp-wrap', ('{#SP#}','[/spoiler]')), # rutracker
    ('(spoiler-head|sp-head)', ('{#SHS#}','{#SHE#}')),
    ('sp-body[^>]* title="([^"]+)', ('{#SHS#}_{#SHE#}','')),
    # quotes
    ('class="q"', ('[quote]','[/quote]')),
    ('class="quote"', ('[quote]','[/quote]')),
    ('class="q" head="([^"]+)', ('[quote="_"]','[/quote]')),
    # code & pre
    ('c-body', ('[code]','[/code]')),
    ('post-pre', ('[font="monospace"]','[/font]')),
)

BANNED_TAGS = ('fieldset', 'style', 'form',)
SKIP_TAGS = ('object', 'param', 'embed', 'script', 'p',)
SKIP_TAGS_ATTR = (
    'display: none', '"heading"', 'colhead',
    'sp-fold', 'q-head', 'c-head', 'sp-title', 'quote-title',
)
TAGS_WITH_URLS = ('a', 'var', 'img',)
# no nesting allowed ([b][b]test[/b][/b] -> [b]test[/b])
BBTAGS_NO_NEST = (
    '[b]', '[i]', '[u]', '[color=_]', '[align=_]', '[size=_]',
)
CLOSED_TAGS = (
    'meta', 'base', 'basefont', 'param', 'frame',
    'link', 'img', 'br', 'hr', 'area', 'input',
)

POOL_SIZE = 10

THUMB_SIZE = (220, 220)
thumb_re = re.compile((r'\[url=(?P<url>{0})\].*?\[img\](?P<th>{0})' +
    '\[/img\].*?\[/url\]').format(DOWNLOAD_URL))

site_root = ''
target_root = ''
output_file = 'out.txt'
urls = {}


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
    return sha1(u.encode('utf-8')).hexdigest()
    
    
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
    for (i,v) in COMPLEX_RULES:
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
        if 'hdclub' in site_root and cltag == '[/color]':
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
                g = decode_html_entities(g)
                g_ = hashurl(g)
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
    print('Processing tags...', end=' ')
    # Reduce the page.
    for p in PSTO_PATTERNS:
        m = re.search(FLAGS + p, s)
        if m:
            s = m.group(1)
            break
    # Cut out bad tags.
    for t in BANNED_TAGS:
        s = s.split('<' + t)[0]
    # Apply simple rules.
    for (k, r) in SIMPLE_RULES:
        s = re.sub(FLAGS + k, r, s)
    # Close tags that should be closed, leave already closed as-is
    for t in CLOSED_TAGS:
        s,n = re.subn(FLAGS + r'<({0}[^>]*?)/?>'.format(t), r'<\1/>', s)
        # Maybe this is overkill, but why not.
        s = s.replace('</{0}>'.format(t), '')
    # Apply complex rules.
    (s, n) = ntag_re.subn(proctag, s)
    m, n = n, 1
    while n > 0:
        (s,n) = ptag_re.subn(proctag, s)
        m += n
    # Strip out any HTML leftovers.
    s = re.sub('<[^>]+>','',s)
    print('done: {0} tags'.format(m))
    
    def print_urls(a, b, p=None):
        if p:
            print('{0}'.format(p.size - p.free_count()), end=' ')
        if a != b:
            print('{0} >> {1}'.format(a, b))
    
    print('Processing {0} URLs...\n'.format(len(urls)))
    # Rehost images.
    if gevent:
        pool = Pool(POOL_SIZE)
        def finale(url):
            def f(g):
                urls[hashurl(url)] = g.value
                print_urls(url, g.value, pool)
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
    
    print('\nFound and replaced {0} images'.format(imgs))
    return decode_html_entities(s).strip()


def postprocess(s):
    """Prettify the bbcode."""
    print('Post-processing...')
    s = '\n'.join([x.strip() for x in s.splitlines()])
    
    # Poster fix: make the poster float to the right
    if any([x in site_root for x in ('epidemz', 'hdclub')]):
        s = re.sub(r'\[img[^]]*\]', r'[img=right]', s, 1)
        print('-- poster fix applied')
    
    # List fix: convert list of "[*]" to proper bulleted list
    if '[list]' not in s and '[*]' in s:
        s, n = re.subn(r'(\[\*\].*)', r'[list]\1[/list]', s)
        if n > 0:
            s = re.sub(r'\[/list\]\s*\[list\]', '', s)
            print('-- list fix applied ({0} items)'.format(n))
    
    # Thumbnail fix: generate thumbnails for linked images
    if Image:
        def thumb(m):
            d = m.groupdict()
            url = d['url']
            old_th = d['th']
            code_origin = m.group()
            code_normal = '[url={0}][img]{1}[/img][/url]'
            tname = 't' + hashurl(url) + '.jpg'
            th = rehost_m.cache_search(tname)
            if th is not None:
                print('.  {0} - from cache'.format(th))
                return code_normal.format(url, th)
            try:
                i = Image.open(open_thing(url)[0])
                if old_th != url:
                    t = Image.open(open_thing(old_th)[0])
                    f1 = float(i.size[1]) / i.size[0]
                    f2 = float(t.size[1]) / t.size[0]
                    if abs(f1 - f2) / (f1 + f2) < 0.02 and t.size[0] >= 180:
                        print('.  {0} - good'.format(old_th))
                        return code_origin
                i.thumbnail(THUMB_SIZE, Image.ANTIALIAS)
                i.save(tname, quality=85)
                th = rehost(tname, force_cache=True)
                os.remove(tname)
            except:
                return code_origin
            print('.  {0} - new'.format(th))
            return code_normal.format(url, th)
        s, n = thumb_re.subn(thumb, s)
        if n > 0:
            print('-- thumbnail fix ({0} checked)'.format(n))
    
    print('Post-processing done')
    return s
    
    
if __name__ == '__main__':
    if not sys.argv[1:]:
        print(__doc__)
        sys.exit()
    if sys.argv[2:]:
        output_file = sys.argv[2]
    
    target = sys.argv[1]
    tp = urlparse(target)
    site_root = urlunparse([tp.scheme, tp.netloc, '', '', '', '',])
    target_root = site_root + re.sub('[^/]*$', '', tp.path)
    
    print('Opening target...', end=' ')
    fd, ftype, finfo = open_thing(target)
    if fd is None:
        sys.exit('\nTerminated: target unreachable.')
    if finfo is not None and finfo.maintype != 'text':
        sys.exit("\nTerminated: cannot parse '{0}'.".format(finfo.maintype))
    print('ok')
    target_charset = 'cp1251'
    if finfo is not None:
        c = finfo.getparam('charset')
        if c is not None:
            target_charset = c
    instr = fd.read().decode(target_charset)
    
    try:
        outstr = process(instr)
    except KeyboardInterrupt as ex:
        sys.exit('\nTerminated manually.')
    
    try:
        outstr = postprocess(outstr)
    except KeyboardInterrupt as ex:
        print('\nPost-processing terminated.')
    
    try:
        open(output_file, 'w').write(outstr.encode('utf-8'))
        print('Output written to', output_file)
    except IOError as ex:
        print('[!] I/O error.', ex)
        sys.exit(1)
    
    if os.name == 'nt': os.startfile(output_file, 'open')
    