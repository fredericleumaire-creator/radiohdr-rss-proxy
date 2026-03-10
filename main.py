import os
import re
import html
import threading
import requests
from flask import Flask, Response, request, jsonify
from xml.etree import ElementTree as ET

app = Flask(__name__)

RSS_URL  = 'https://anchor.fm/s/1016b2f68/podcast/rss'

# ─── Cache en mémoire ──────────────────────────────────────────────────────────
image_cache: dict[str, bytes] = {}   # url → bytes
rss_cache:   dict             = {}   # {'items': [...], 'ts': float}
cache_lock   = threading.Lock()

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36',
    'Referer':    'https://open.spotify.com/',
}

# ─── Téléchargement image en cache ────────────────────────────────────────────
def fetch_image_cached(url: str) -> bytes | None:
    with cache_lock:
        if url in image_cache:
            return image_cache[url]
    try:
        r = requests.get(url, timeout=15, headers=HEADERS)
        if r.status_code == 200:
            with cache_lock:
                image_cache[url] = r.content
            return r.content
    except Exception:
        pass
    return None

def prefetch_images(urls: list[str]):
    """Précharge les images en arrière-plan."""
    for url in urls:
        if url and url not in image_cache:
            fetch_image_cached(url)

# ─── Proxy image ───────────────────────────────────────────────────────────────
@app.route('/image')
def proxy_image():
    url = request.args.get('url')
    if not url:
        return Response('Missing url', status=400)
    data = fetch_image_cached(url)
    if data:
        return Response(data, content_type='image/jpeg',
                        headers={'Cache-Control': 'public, max-age=86400'})
    return Response('Image unavailable', status=502)

# ─── RSS parsé en JSON ─────────────────────────────────────────────────────────
@app.route('/rss')
def proxy_rss():
    import time
    with cache_lock:
        cached = rss_cache.get('data')
        ts     = rss_cache.get('ts', 0)
    # Cache RSS valide 30 minutes
    if cached and (time.time() - ts) < 1800:
        return jsonify(cached)

    try:
        r = requests.get(RSS_URL, timeout=20, headers={'User-Agent': 'Mozilla/5.0'})
        r.raise_for_status()
        xml = r.text

        root    = ET.fromstring(xml.encode('utf-8'))
        ns      = {'itunes': 'http://www.itunes.com/dtds/podcast-1.0.dtd'}
        channel = root.find('channel')

        # Image podcast fallback
        podcast_image = None
        img_el = channel.find('itunes:image', ns)
        if img_el is not None:
            podcast_image = img_el.get('href') or img_el.text

        base_url = os.environ.get('RAILWAY_PUBLIC_DOMAIN', 'localhost:5000')
        base_url = f"https://{base_url}" if not base_url.startswith('http') else base_url

        def proxy_url(u):
            if not u:
                return None
            return f"{base_url}/image?url={requests.utils.quote(u, safe='')}"

        items = []
        for item in channel.findall('item'):
            def get(tag):
                el = item.find(tag) or item.find(tag, ns)
                return (el.text or '').strip() if el is not None else ''

            titre_raw = get('title').replace('<![CDATA[', '').replace(']]>', '').strip()

            # Extraction émission / sousTitre
            if re.search(r'interview', titre_raw, re.IGNORECASE):
                emission   = 'Interview'
                sous_titre = re.sub(r'interview', '', titre_raw, flags=re.IGNORECASE).lstrip(' :-').strip()
            else:
                sep = re.match(r'^(.+?)(?:\s+-\s+|:)(.*)$', titre_raw)
                if sep:
                    emission   = sep.group(1).strip()
                    sous_titre = sep.group(2).strip()
                else:
                    emission   = titre_raw
                    sous_titre = ''

            # Audio
            enclosure = item.find('enclosure')
            audio_url = enclosure.get('url') if enclosure is not None else None
            if not audio_url:
                continue

            # Pochette
            img_item    = item.find('itunes:image', ns)
            pochette_raw = img_item.get('href') if img_item is not None else None
            pochette    = proxy_url(pochette_raw or podcast_image)

            # Durée
            duree_el  = item.find('itunes:duration', ns)
            duree_raw = (duree_el.text or '').strip() if duree_el is not None else ''
            if duree_raw.isdigit():
                secs = int(duree_raw)
                h, m, s = secs//3600, (secs%3600)//60, secs%60
                duree = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
            else:
                duree = duree_raw

            # Description
            desc_el  = item.find('description') or item.find('{http://www.itunes.com/dtds/podcast-1.0.dtd}summary')
            desc_raw = (desc_el.text or '') if desc_el is not None else ''
            desc_raw = desc_raw.replace('<![CDATA[', '').replace(']]>', '')
            description = html.unescape(re.sub('<[^>]+>', '', desc_raw)).strip()

            items.append({
                'id':          f"rss-{len(items)}",
                'titre':       titre_raw,
                'emission':    emission,
                'sousTitre':   sous_titre,
                'description': description,
                'date':        get('pubDate'),
                'duree':       duree,
                'pochette':    pochette,
                'audioUrl':    audio_url,
            })

        # Propager pochettes par émission
        pochettes = {}
        for it in items:
            if it['pochette'] and it['emission']:
                pochettes[it['emission']] = it['pochette']
        for it in items:
            if not it['pochette'] and it['emission'] in pochettes:
                it['pochette'] = pochettes[it['emission']]

        result = {'items': items, 'count': len(items)}

        # Mettre en cache RSS
        import time
        with cache_lock:
            rss_cache['data'] = result
            rss_cache['ts']   = time.time()

        # Précharger toutes les images en arrière-plan
        urls = list({it['pochette'].split('url=')[-1] for it in items if it['pochette'] and 'url=' in it['pochette']})
        threading.Thread(target=prefetch_images, args=(urls,), daemon=True).start()

        return jsonify(result)

    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ─── Health check ──────────────────────────────────────────────────────────────
@app.route('/')
def health():
    return jsonify({'status': 'ok', 'service': 'radiohdr-rss-proxy', 'cached_images': len(image_cache)})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
