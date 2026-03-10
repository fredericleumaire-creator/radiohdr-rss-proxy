import os, re, html, time, threading, requests
from flask import Flask, Response, request, jsonify
from xml.etree import ElementTree as ET

app     = Flask(__name__)
RSS_URL = 'https://anchor.fm/s/1016b2f68/podcast/rss'
HEADERS = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://open.spotify.com/'}

rss_cache  = {}
cache_lock = threading.Lock()

# ─── Proxy image (passe-plat simple) ──────────────────────────────────────────
@app.route('/image')
def proxy_image():
    url = request.args.get('url')
    if not url:
        return Response('Missing url', status=400)
    try:
        r = requests.get(url, timeout=10, headers=HEADERS)
        return Response(r.content,
                        content_type=r.headers.get('Content-Type', 'image/jpeg'),
                        headers={'Cache-Control': 'public, max-age=86400'})
    except Exception as e:
        return Response(str(e), status=502)

# ─── RSS parsé en JSON (cache 30min) ──────────────────────────────────────────
@app.route('/rss')
def proxy_rss():
    with cache_lock:
        cached = rss_cache.get('data')
        ts     = rss_cache.get('ts', 0)
    if cached and (time.time() - ts) < 1800:
        return jsonify(cached)
    try:
        r = requests.get(RSS_URL, timeout=20, headers={'User-Agent': 'Mozilla/5.0'})
        r.raise_for_status()
        root    = ET.fromstring(r.text.encode('utf-8'))
        ns      = {'itunes': 'http://www.itunes.com/dtds/podcast-1.0.dtd'}
        channel = root.find('channel')

        img_el        = channel.find('itunes:image', ns)
        podcast_image = img_el.get('href') if img_el is not None else None

        base = os.environ.get('RAILWAY_PUBLIC_DOMAIN', 'localhost:5000')
        base = f"https://{base}" if not base.startswith('http') else base

        def px(u):
            return f"{base}/image?url={requests.utils.quote(u, safe='')}" if u else None

        items = []
        for item in channel.findall('item'):
            def g(tag):
                el = item.find(tag) or item.find(tag, ns)
                return (el.text or '').strip() if el is not None else ''

            titre = g('title').replace('<![CDATA[', '').replace(']]>', '').strip()

            if re.search(r'interview', titre, re.IGNORECASE):
                emission, sous = 'Interview', re.sub(r'interview', '', titre, flags=re.IGNORECASE).lstrip(' :-').strip()
            else:
                m = re.match(r'^(.+?)(?:\s+-\s+|:)(.*)$', titre)
                emission, sous = (m.group(1).strip(), m.group(2).strip()) if m else (titre, '')

            enc = item.find('enclosure')
            if enc is None: continue
            audio = enc.get('url')

            img       = item.find('itunes:image', ns)
            pochette  = px((img.get('href') if img is not None else None) or podcast_image)

            dr = item.find('itunes:duration', ns)
            d  = (dr.text or '').strip() if dr is not None else ''
            if d.isdigit():
                s = int(d); h, m2, s2 = s//3600, (s%3600)//60, s%60
                d = f"{h}:{m2:02d}:{s2:02d}" if h else f"{m2}:{s2:02d}"

            de = item.find('description')
            desc = html.unescape(re.sub('<[^>]+>', '', (de.text or '').replace('<![CDATA[','').replace(']]>',''))).strip() if de is not None else ''

            items.append({'id': f"rss-{len(items)}", 'titre': titre, 'emission': emission,
                          'sousTitre': sous, 'description': desc, 'date': g('pubDate'),
                          'duree': d, 'pochette': pochette, 'audioUrl': audio})

        # Propager pochettes par émission
        poch = {it['emission']: it['pochette'] for it in items if it['pochette']}
        for it in items:
            if not it['pochette']: it['pochette'] = poch.get(it['emission'])

        result = {'items': items, 'count': len(items)}
        with cache_lock:
            rss_cache['data'], rss_cache['ts'] = result, time.time()
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/')
def health():
    return jsonify({'status': 'ok', 'service': 'radiohdr-rss-proxy'})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
