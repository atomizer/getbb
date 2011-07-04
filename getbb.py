#!/usr/bin/python
# 2010 atomizer
# version 0.10

from __future__ import print_function

import sys
import os
import re
import argparse
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
    Image = None

try:
    import chardet
except ImportError:
    chardet = None

import rehost as rehost_m
from rehost import *

FLAGS = '(?si)'
# self-closed tags
ntag_re = re.compile(FLAGS +
    '<(?P<tag>\w+)(?P<attr>[^>]*)(?P<content>)/>')
# paired tags
ptag_re = re.compile(FLAGS +
    '<(?P<tag>\w+)(?P<attr>[^>]*)>(?P<content>[^<]*)</\w+>')

PSTO_PATTERNS = (
    'class="post_?body"[^>]*>(.*?)(?:</div><!--/post_body|<!-- //bt)',  # rutracker-alike
    '>[0-9a-f]{40}</td></tr>(.*?)<a name="startcomments">',  # hdclub-alike
    'class="heading_b"[^>]*>(.*?)</table>',  # hdclub
    'id="news-id-[^>]*>(.*?)</p>',  # epidemz
    'id=\'news-id-[^>]*>(.*?)<td class="j"', # very secret site
)
SIMPLE_RULES = (
    ('\n', ''), ('\r', ''), ('<wbr>', ''), (r'<!(\s*--.*?--\s*)*>', ''),
    # line breaks, horisontal rulers
    ('<span class="post-br">.*?</span>', '\n\n'),
    ('<span class="post-hr">.*?</span>', '[hr]'),
    ('<hr[^>]*>', '[hr]'),
    ('<br[^>]*>', '\n'),
    ('<div></div>', '\n'),
    ('<tr[^>]*>', ''), ('</tr>', '\n'),
    # lists
    ('<[ou]l[^>]*>', '[list]'), ('</[ou]l>', '[/list]'),
    ('<[ou]l type="([^"])">', '[list=\\1]'),
    ('<li[^>]*>', '[*]'),  ('</li>', ''),
    # hdclub & epidemz dumb tags
    ('<b>', '[b]'), ('</b>', '[/b]'),
    ('<i>', '[i]'), ('</i>', '[/i]'),
    ('<u>', '[u]'), ('</u>', '[/u]'),
    # hdclub's textarea
    ('<textarea>', '[font="monospace"]'),
    ('</textarea>', '[/font]'),
    # center, huh
    ('<center>', '[align=center]'), ('</center>', '[/align]'),
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
    ('href=[\'"]([^\'"]+)', ('[url=_]','[/url]')),
    # images
    ('src=[\'"]([^\'"]+)', ('[img]','[/img]')),
    ('class="postImg" title="([^"]+)', ('[img]','[/img]')),
    ('class="postImg [^"]*?img-([^ "]*)[^>]*?title="([^"]+)',
        ('[img=_]','[/img]')),
    # align
    ('float: ?(left|right)', ('{#FLOAT#}','')),
    ('text-align: ?([^;"]+)', ('[align=_]', '[/align]')),
    (' align="([^"]+)', ('[align=_]', '[/align]')), # hdclub, epidemz
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

SKIP_TAGS = (
    'object', 'param', 'embed', 'form',
    'script', 'style', 'head', 'p', 'noindex', 'noscript',
)
SKIP_TAGS_ATTR = (
    'display: ?none', '"heading"', 'colhead',
    'sp-fold', 'q-head', 'c-head', 'sp-title', 'quote-title',
    'attach', 'thx-container', 'tor-fl-wrap',
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
    for t in SKIP_TAGS_ATTR:
        if re.search(t, d['attr']):
            return ''
    dc = d['content']
    
    # very secret site, fucked up
    if 'dvdtalk.ru' in site_root:
        if 'class="z"' in d['attr'] or 'width="190"' in d['attr']:
            dc = dc.replace('\n', ' ')
        if d['tag'] == 'span':
            return '[b]' + dc + '[/b]'
    
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
        # ban comic sans
        if cltag == '[/font]' and g == "'Comic Sans MS'":
            return dc
        # align=left is pointless
        if cltag == '[/align]' and g == 'left':
            return dc
        
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
    return dc


def process(s):
    global urls
    urls = {}
    # Hackity hack.
    s = s.split('class="attach')[0].split('<')
    s.pop()
    s = '<'.join(s)
    # Cut out bad tags.
    for t in SKIP_TAGS:
        s = re.sub(FLAGS + '\s*<(?P<tag>' + t + ').*?</(?P=tag)>\s*', '', s)
    # Apply simple rules.
    for (k, r) in SIMPLE_RULES:
        s = re.sub(FLAGS + k, r, s)
    # Close tags that should be closed, leave already closed as-is
    for t in CLOSED_TAGS:
        s = re.sub(FLAGS + r'<({0}[^>]*?)/?>'.format(t), r'<\1/>', s)
        # Maybe this is overkill, but why not.
        s = s.replace('</{0}>'.format(t), '')
    # Apply complex rules.
    (s, n) = ntag_re.subn(proctag, s)
    m, n = n, 1
    while n > 0:
        (s, n) = ptag_re.subn(proctag, s)
        m += n
    # Strip out any HTML leftovers.
    s = re.sub('<[^>]+>','',s)
    if m > 0:
        print('Replaced {0} tags'.format(m))
    
    if not args.no_rehost and len(urls) > 0:
        def print_urls(a, b):
            if a != b:
                print('{0} >> {1}'.format(a, b))
        print('Processing {0} URLs...'.format(len(urls)))
        # Rehost images.
        if gevent:
            pool = Pool(POOL_SIZE)
            def fin(h, url):
                def f(g):
                    urls[h] = g.value
                    print_urls(url, g.value)
                return f
            for h, url in urls.iteritems():
                j = pool.spawn(rehost, url, image=True, referer=target_root)
                j.link_value(fin(h, url))
            pool.join()
        else:
            for h, url in urls.iteritems():
                new_url = rehost(url, image=True, referer=target_root)
                urls[h] = new_url
                print_urls(url, new_url)
    # Bring URLs back in places.
    imgs = 0
    for p, url in urls.iteritems():
        if hashurl(url) != p:
            imgs += 1
        s = s.replace(p, urls[p])
    if imgs > 0:
        print('Found and replaced {0} images'.format(imgs))
    return decode_html_entities(s).strip()


def postprocess(s):
    """Prettify the bbcode."""
    print('Post-processing...')
    
    # Poster fix: make the poster float to the right
    if any([x in site_root for x in ('epidemz', 'hdclub')]) or s[:5] == '[img]':
        s = re.sub(r'\[img[^]]*\]', r'[img=right]', s, 1)
        print('-- poster fix applied')
    
    # List fix: convert list of "[*]" to proper bulleted list
    if '[list]' not in s and '[*]' in s:
        s, n = re.subn(r'(\[\*\].*)', r'[list]\1[/list]', s)
        if n > 0:
            s = re.sub(r'\[/list\]\s*\[list\]', '', s)
            print('-- list fix applied ({0} items)'.format(n))
    
    # Thumbnail fix: generate thumbnails for linked images
    if not args.no_thumb and Image:
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
                        rehost_m.cache_write(tname, old_th)
                        return code_origin
                i.thumbnail(THUMB_SIZE, Image.ANTIALIAS)
                i.save(tname, quality=85)
            except IOError as ex:
                print(ex)
                return code_origin
            th = rehost(tname, force_cache=True)
            try:
                os.unlink(tname)
            except:
                pass
            print('.  {0} - new'.format(th))
            return code_normal.format(url, th)
        s, n = thumb_re.subn(thumb, s)
        if n > 0:
            print('-- thumbnail fix ({0} checked)'.format(n))
    
    surround = ('/?quote(="[^"]*")?', '/?spoiler(="[^"]*")?', '/?list', 'hr')
    for t in surround:
        s = re.sub(r'\s*(\[' + t + r'\])\s*', r'\n\1\n', s)
    s = '\n'.join([x.strip() for x in s.splitlines()])
    print('Post-processing done')
    return s


if __name__ == '__main__':
    p = argparse.ArgumentParser(
        description='Decompile HTML to BBCode',
        epilog='Latest version and more info at https://github.com/atomizer/getbb'
    )
    p.add_argument(
        'target', help='local file or URL to be parsed'
    )
    p.add_argument(
        '-o', dest='output',
        default=os.path.join(os.path.abspath(os.path.dirname(__file__)), 'out.txt'),
        type=argparse.FileType('w'),
        help='write output to file (default: %(default)s)'
    )
    p.add_argument(
        '-c', metavar='N', dest='count', type=int, default=1,
        help='parse N consecutive posts (default: 1)'
    )
    p.add_argument(
        '-C', dest='charset', type=str, default='',
        help='set input charset (default: auto-detect)'
    )
    p.add_argument(
        '-nr', '--no-rehost', action='store_true',
        help='leave URLs as-is'
    )
    p.add_argument(
        '-nt', '--no-thumb', action='store_true',
        help='don\'t fix bad thumbnails'
    )
    p.add_argument(
        '-no', '--no-open', action='store_true',
        help='don\'t open output file in notepad (Windows)'
    )
    args = p.parse_args()
    
    
    if not os.path.isfile(args.target):
        tp = urlparse(args.target)
        site_root = urlunparse([tp.scheme, tp.netloc] + [''] * 4)
        target_root = site_root + re.sub('[^/]*$', '', tp.path)
    else:
        site_root = ''
        target_root = ''
    
    
    print('Opening target...', end=' ')
    fd, ftype, finfo = open_thing(args.target)
    if fd is None:
        sys.exit('Terminated: target unreachable.')
    if finfo is not None:
        if finfo.maintype != 'text':
            sys.exit('Terminated: cannot parse "{0}".'.format(finfo.maintype))
        if finfo.url != args.target and 'login' in finfo.url:
            sys.exit('Terminated: redirected to login page.\n' +
                'Try to save the page from your browser and pass the file.')
    print('ok')
    inbytes = fd.read()
    # Detect charset and decode input.
    target_charset = args.charset
    instr = ''
    if not target_charset and finfo is not None:
        target_charset = finfo.getparam('charset')
    if not target_charset:
        if chardet is not None:
            target_charset = chardet.detect(inbytes)['encoding']
        else:
            target_charset = 'cp1251'
    for c in [target_charset] + [x for x in ['cp1251', 'utf-8'] if x != target_charset]:
        print('decoding as {0}...'.format(c), end=' ')
        try:
            instr = inbytes.decode(c)
            print('ok')
            break
        except:
            print('failed')
    if not instr:
        sys.exit('Terminated: nothing to parse.')
    # Extract posts.
    for p in PSTO_PATTERNS:
        m = re.findall(FLAGS + p, instr)
        if len(m) > 0:
            m = m[:args.count]
            break
    if len(m) == 0:
        print('\n[!] Warning: no pattern for this page detected - parsing whole page!' +
            '\n[!] You may want to ask the author for new pattern' +
            ' to reduce amount of shit in the output.\n')
        m = [instr]
    outs = []
    try:
        for i, p in enumerate(m):
            if len(m) > 1:
                print('Post {0}/{1}'.format(i+1, len(m)))
            outs += [process(p)]
    except KeyboardInterrupt as ex:
        sys.exit('\nTerminated manually.')
    outstr = '\n\n'.join(outs)
    try:
        outstr = postprocess(outstr)
    except KeyboardInterrupt as ex:
        print('\nPost-processing terminated.')
    
    try:
        args.output.write(outstr.encode('utf-8'))
        print('Output written to', args.output.name)
    except IOError as ex:
        print('[!] I/O error.', ex)
        sys.exit(1)
    
    if not args.no_open and os.path.isfile(args.output.name) and os.name == 'nt':
        os.startfile(args.output.name, 'open')
