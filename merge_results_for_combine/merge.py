#!/usr/bin/env python3
"""
merge_recon.py — объединяет результаты всех инструментов в один JSON по домену.

Sources:
  - httpx.jsonl       (JSONL, один объект на домен)
  - gospider/         (папка с файлами gospider)
  - js_analysis_report.json
  - ffuf/             (папка с файлами ffuf, имя: https_domain_method.json)

Usage:
  python3 merge_recon.py \
    --httpx httpx.jsonl \
    --gospider ./gospider \
    --js js_analysis_report.json \
    --ffuf ./ffuf \
    --output final_report.json
"""

import json
import re
import sys
import argparse
from pathlib import Path
from urllib.parse import urlparse

# ─── Хелперы ─────────────────────────────────────────────────────────────────

def extract_domain(url: str) -> str:
    """Извлекаем hostname из URL."""
    try:
        return urlparse(url).netloc or url
    except Exception:
        return url


def normalize_domain(domain: str) -> str:
    """Убираем порт если есть."""
    return domain.split(':')[0].lower().strip()


# ─── Парсеры источников ──────────────────────────────────────────────────────

def parse_httpx(filepath: str) -> dict:
    """JSONL файл httpx — один объект на строку."""
    result = {}
    if not filepath or not Path(filepath).exists():
        return result

    for line in Path(filepath).read_text(errors='ignore').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            domain = normalize_domain(d.get('host') or extract_domain(d.get('url', '')))
            if not domain:
                continue
            result[domain] = {
                'url': d.get('url'),
                'status_code': d.get('status_code'),
                'title': d.get('title'),
                'webserver': d.get('webserver'),
                'tech': d.get('tech', []),
                'ip': d.get('host_ip'),
                'a_records': d.get('a', []),
                'cname': d.get('cname', []),
                'content_type': d.get('content_type'),
                'content_length': d.get('content_length'),
                'scheme': d.get('scheme'),
                'port': d.get('port'),
                'words': d.get('words'),
                'lines': d.get('lines'),
            }
        except Exception:
            continue

    return result


SKIP_EXTS = {
    '.css', '.eot', '.ttf', '.woff', '.woff2', '.otf',
    '.png', '.jpg', '.jpeg', '.gif', '.ico', '.svg',
    '.pdf', '.mp4', '.mp3', '.map',
}

INTERESTING_PATTERNS = [
    r'/api/', r'/admin', r'/login', r'/auth', r'/upload',
    r'/download', r'/file', r'/backup', r'/config', r'/debug',
    r'/test', r'/dev', r'/internal', r'/private', r'/v\d+/',
    r'/graphql', r'/webhook', r'/callback',
    r'\.php', r'\.json', r'\.xml', r'\.env',
]

PARAM_PATTERNS = [r'\?.*=', r'\?.*\{.*\}', r'\?s=', r'\?p=\d', r'\?id=', r'\?q=']

SKIP_PATTERNS = [
    r'/wp-content/uploads/',
    r'\.(css|eot|ttf|woff|woff2|otf|png|jpg|jpeg|gif|ico|svg|pdf|mp4|mp3)(\?|$)',
    r'utm_source', r'utm_campaign',
    r'/wp-json/wp/v2/posts/\d+',
    r'/wp-json/wp/v2/pages/\d+',
    r'/wp-json/wp/v2/media/\d+',
    r'/wp-json/wp/v2/categories/\d+',
    r'/wp-json/wp/v2/tags/\d+',
    r'/wp-json/wp/v2/users/\d+',
]

CDN_DOMAINS = [
    'googleapis.com', 'cloudflare.com', 'bootstrapcdn.com',
    'jquery.com', 'cdnjs.com', 'unpkg.com', 'jsdelivr.net',
]


def is_skip(url: str) -> bool:
    path = urlparse(url).path.lower()
    ext = Path(path).suffix.lower()
    if ext in SKIP_EXTS:
        return True
    for pat in SKIP_PATTERNS:
        if re.search(pat, url.lower()):
            return True
    return False


def is_param(url: str) -> bool:
    if not urlparse(url).query or is_skip(url):
        return False
    for pat in PARAM_PATTERNS:
        if re.search(pat, url.lower()):
            return True
    return False


def is_interesting(url: str) -> bool:
    if is_skip(url):
        return False
    for pat in INTERESTING_PATTERNS:
        if re.search(pat, urlparse(url).path.lower()):
            return True
    return False


def is_cdn(host: str) -> bool:
    return any(cdn in host for cdn in CDN_DOMAINS)


def parse_gospider_dir(dirpath: str) -> dict:
    """Папка с файлами gospider."""
    result = {}
    if not dirpath or not Path(dirpath).is_dir():
        return result

    for filepath in sorted(Path(dirpath).iterdir()):
        if not filepath.is_file():
            continue

        domain = normalize_domain(filepath.name.replace('_', '.'))
        urls, js_files, forms = set(), set(), set()

        for line in filepath.read_text(errors='ignore').splitlines():
            line = line.strip()
            if not line:
                continue

            output, typ = None, None

            if line.startswith('{'):
                try:
                    d = json.loads(line)
                    output = d.get('output', '')
                    typ = d.get('type', '')
                except Exception:
                    continue
            else:
                m = re.search(r'https?://\S+', line)
                if m:
                    output = m.group()
                    typ = 'url'

            if not output:
                continue

            # Чистим мусор с конца URL
            output = output.rstrip('",\\')
            if not output.startswith('http'):
                continue

            ext = Path(urlparse(output).path).suffix.lower()
            if typ == 'form':
                forms.add(output)
            elif ext == '.js':
                js_files.add(output)
            else:
                urls.add(output)

        # Фильтруем
        params = sorted({u for u in urls if is_param(u)})
        interesting = sorted({u for u in urls if is_interesting(u) and not is_param(u)})

        # JS
        root = re.sub(r'^www\.', '', domain)
        own_js, third_js = [], []
        for url in js_files:
            host = re.sub(r'^www\.', '', urlparse(url).netloc)
            if root in host:
                own_js.append(url)
            elif not is_cdn(host):
                third_js.append(url)

        # Дедупликация по пути без query string
        seen_paths = set()
        deduped_own = []
        for url in sorted(own_js):
            p = urlparse(url)
            if p.path not in seen_paths:
                seen_paths.add(p.path)
                deduped_own.append(f"{p.scheme}://{p.netloc}{p.path}")
        own_js = deduped_own

        seen_paths = set()
        deduped_third = []
        for url in sorted(third_js):
            p = urlparse(url)
            if p.path not in seen_paths:
                seen_paths.add(p.path)
                deduped_third.append(f"{p.scheme}://{p.netloc}{p.path}")
        third_js = deduped_third

        brut_dirs = sorted({
            urlparse(u).path.rsplit('/', 1)[0] + '/'
            for u in own_js
        })

        result[domain] = {
            'urls': interesting,
            'params': params,
            'js': {
                'own': own_js,
                'third_party': sorted(set(third_js)),
            },
            'brut_dirs': brut_dirs,
            'forms': sorted(forms),
        }

    return result


def parse_js_analysis(filepath: str) -> dict:
    """js_analysis_report.json — группируем находки по домену."""
    result = {}
    if not filepath or not Path(filepath).exists():
        return result

    data = json.loads(Path(filepath).read_text(encoding='utf-8'))

    def collect_by_domain(category_data: dict, category_name: str):
        for finding_type, findings in category_data.items():
            for f in findings:
                domain = normalize_domain(f.get('domain', ''))
                if not domain:
                    continue
                if domain not in result:
                    result[domain] = {'secrets': [], 'apis': [], 'sensitive': []}
                result[domain][category_name].append({
                    'type': finding_type,
                    'value': f.get('value'),
                    'file': f.get('file'),
                    'line_no': f.get('line_no'),
                    'context': f.get('context'),
                })

    collect_by_domain(data.get('secrets', {}), 'secrets')
    collect_by_domain(data.get('apis', {}), 'apis')
    collect_by_domain(data.get('sensitive', {}), 'sensitive')

    return result


def parse_ffuf_dir(dirpath: str) -> dict:
    """
    Папка с файлами ffuf.
    Имя файла: https_domain_com_get.json или https_domain_com_post.json
    """
    result = {}
    if not dirpath or not Path(dirpath).is_dir():
        return result

    for filepath in sorted(Path(dirpath).glob('http*.json')):
        # Парсим имя файла для домена и метода
        name = filepath.stem  # https_webmail_odrex_pw_get
        parts = name.split('_')

        # Метод из имени файла
        if parts[-1].lower() in ('get', 'post', 'put', 'delete'):
            method = parts[-1].upper()
        else:
            method = 'GET'

        try:
            data = json.loads(filepath.read_text(encoding='utf-8'))
        except Exception:
            continue

        if isinstance(data, list):
            continue

        # Берём домен из результатов или commandline
        results = data.get('results', [])
        if results:
            domain = normalize_domain(results[0].get('host', ''))
        else:
            cmd = data.get('commandline', '')
            m = re.search(r'https?://([^/\s]+)', cmd)
            domain = normalize_domain(m.group(1)) if m else ''

        if not domain:
            continue

        findings = []
        for r in data.get('results', []):
            findings.append({
                'url': r.get('url'),
                'status': r.get('status'),
                'length': r.get('length'),
                'words': r.get('words'),
                'lines': r.get('lines'),
                'redirect': r.get('redirectlocation') or None,
                'content_type': r.get('content-type'),
            })

        if domain not in result:
            result[domain] = {}
        result[domain][method] = findings

    return result


# ─── Merger ──────────────────────────────────────────────────────────────────

def merge(httpx_data, gospider_data, js_data, ffuf_data) -> dict:
    # Собираем все домены из всех источников
    all_domains = set()
    all_domains.update(httpx_data.keys())
    all_domains.update(gospider_data.keys())
    all_domains.update(js_data.keys())
    all_domains.update(ffuf_data.keys())

    result = {}
    for domain in sorted(all_domains):
        result[domain] = {
            'httpx': httpx_data.get(domain),
            'gospider': gospider_data.get(domain),
            'js_analysis': js_data.get(domain),
            'ffuf': ffuf_data.get(domain),
        }

    return result


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Merge recon results into one JSON')
    parser.add_argument('--httpx', help='Path to httpx JSONL file')
    parser.add_argument('--gospider', help='Path to gospider output directory')
    parser.add_argument('--js', help='Path to js_analysis_report.json')
    parser.add_argument('--ffuf', help='Path to ffuf output directory')
    parser.add_argument('--output', default='final_report.json', help='Output file')
    args = parser.parse_args()

    print("[*] Parsing httpx...")
    httpx_data = parse_httpx(args.httpx)
    print(f"    {len(httpx_data)} domains")

    print("[*] Parsing gospider...")
    gospider_data = parse_gospider_dir(args.gospider)
    print(f"    {len(gospider_data)} domains")

    print("[*] Parsing JS analysis...")
    js_data = parse_js_analysis(args.js)
    print(f"    {len(js_data)} domains")

    print("[*] Parsing ffuf...")
    ffuf_data = parse_ffuf_dir(args.ffuf)
    print(f"    {len(ffuf_data)} domains")

    print("[*] Merging...")
    final = merge(httpx_data, gospider_data, js_data, ffuf_data)

    Path(args.output).write_text(
        json.dumps(final, indent=2, ensure_ascii=False),
        encoding='utf-8'
    )

    print(f"[+] Done! {len(final)} domains → {args.output}")


if __name__ == '__main__':
    main()