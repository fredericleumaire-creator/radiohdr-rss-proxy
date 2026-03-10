import os
import requests
from flask import Flask, Response, request, jsonify
from xml.etree import ElementTree as ET

app = Flask(__name__)

RSS_URL = 'https://anchor.fm/s/1016b2f68/podcast/rss'

# ─── Proxy image ───────────────────────────────────────────────────────────────
@app.route('/image')
def proxy_image():
    url = request.args.get('url')
    if not url:
        return Response('Missing url', status=400)
    try:
        r = requests.get(url, timeout=10, headers={
            'User-Agent': 'Mozilla/5.0',
            'Referer':    'https://open.spotify.com/',
        })
        return Response(
            r.content,
            content_type=r.headers.get('Content-Type', 'image/jpeg'),
            headers={'Cache-Control': 'public, max-age=86400'}
        )
    except Exception as e:
        return Response(str(e), status=502)

# ─── RSS parsé en JSON ─────────────────────────────────────────────────────────
@app.route('/rss')
def proxy_rss():
    try:
        r = requests.get(RSS_URL, timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
        r.raise_for_status()
        xml = r.text

        # Image podcast fallback
        root = ET.fromstring(xml.encode('utf-8'))
        ns = {'itunes': 'http://www.itunes.com/dtds/podcast-1.0.dtd'}
        channel = root.find('channel')

        podcast_image = None
        img_el = channel.find('itunes:image', ns)
        if img_el is not None:
            podcast_image = img_el.get('href') or img_el.text

        base_url = os.environ.get('RAILWAY_PUBLIC_DOMAIN', 'localhost:5000')
        base_url = f"https://{base_url}" if not base_url.startswith('http') else base_url

        def proxy_img(url):
            if not url:
                return None
            return f"{base_url}/image?url={requests.utils.quote(url, safe='')}"

        items = []
        for item in channel.findall('item'):
            def get(tag, attr=None):
                el = item.find(tag) or item.find(tag, ns)
                if el is None:
                    return None
                if attr:
                    return el.get(attr)
                return (el.text or '').strip()

            titre_raw = get('title') or ''
            # Nettoyage CDATA
            titre_raw = titre_raw.replace('<![CDATA[', '').replace(']]>', '').strip()

            # Extraction émission / sousTitre
            import re
            if re.search(r'interview', titre_raw, re.IGNORECASE):
                emission  = 'Interview'
                sous_titre = re.sub(r'interview', '', titre_raw, flags=re.IGNORECASE).lstrip(' :-').strip()
            else:
                sep = re.match(r'^(.+?)(?:\s+-\s+|:)(.*)$', titre_raw)
                if sep:
                    emission   = sep.group(1).strip()
                    sous_titre = sep.group(2).strip()
                else:
                    emission   = titre_raw
                    sous_titre = ''

            # Audio URL
            enclosure = item.find('enclosure')
            audio_url = enclosure.get('url') if enclosure is not None else None

            if not audio_url:
                continue

            # Pochette épisode
            img_item = item.find('itunes:image', ns)
            pochette_raw = img_item.get('href') if img_item is not None else None
            pochette = proxy_img(pochette_raw or podcast_image)

            # Date
            pub_date = get('pubDate') or ''

            # Durée
            duree_el = item.find('itunes:duration', ns)
            duree_raw = (duree_el.text or '').strip() if duree_el is not None else ''
            if duree_raw.isdigit():
                secs = int(duree_raw)
                h, m, s = secs//3600, (secs%3600)//60, secs%60
                duree = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
            else:
                duree = duree_raw

            # Description
            desc_el = item.find('description') or item.find('{http://www.itunes.com/dtds/podcast-1.0.dtd}summary')
            desc_raw = (desc_el.text or '') if desc_el is not None else ''
            desc_raw = desc_raw.replace('<![CDATA[', '').replace(']]>', '')
            import html
            description = html.unescape(re.sub('<[^>]+>', '', desc_raw)).strip()

            items.append({
                'id':          f"rss-{len(items)}",
                'titre':       titre_raw,
                'emission':    emission,
                'sousTitre':   sous_titre,
                'description': description,
                'date':        pub_date,
                'duree':       duree,
                'pochette':    pochette,
                'audioUrl':    audio_url,
            })

        # Propager pochettes manquantes par émission
        pochettes = {}
        for it in items:
            if it['pochette'] and it['emission']:
                pochettes[it['emission']] = it['pochette']
        for it in items:
            if not it['pochette'] and it['emission'] in pochettes:
                it['pochette'] = pochettes[it['emission']]

        return jsonify({'items': items, 'count': len(items)})

    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ─── Health check ──────────────────────────────────────────────────────────────
@app.route('/')
def health():
    return jsonify({'status': 'ok', 'service': 'radiohdr-rss-proxy'})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
