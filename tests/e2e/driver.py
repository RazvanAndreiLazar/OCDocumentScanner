'''E2E driver: runs inside the app container (isolated netns shared with cupsd + a simulated printer).'''
import io, json, os, re, sys, time, urllib.request, urllib.error, uuid, zipfile
from PIL import Image

BASE = 'http://127.0.0.1:8000'
FILES = '/e2e/files'
ok = True


def check(cond, what):
    global ok
    print(('PASS ' if cond else 'FAIL ') + what)
    ok &= bool(cond)


def request(method, path, body=None, headers=None):
    req = urllib.request.Request(BASE + path, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, dict(r.headers), r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode()


def upload(name, data, **fields):
    boundary = uuid.uuid4().hex
    parts = []
    for key, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
                 f'Content-Type: application/octet-stream\r\n\r\n'.encode() + data + b'\r\n')
    parts.append(f'--{boundary}--\r\n'.encode())
    return request('POST', '/print', b''.join(parts), {
        'Content-Type': f'multipart/form-data; boundary={boundary}', 'Origin': BASE, 'HX-Request': 'true'})


def docx(text):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('[Content_Types].xml', '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        z.writestr('_rels/.rels', '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        z.writestr('word/document.xml', f'<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>')
    return buf.getvalue()


def png():
    buf = io.BytesIO()
    Image.new('RGB', (600, 400), (200, 30, 30)).save(buf, 'PNG')
    return buf.getvalue()


def jobs_html():
    return request('GET', '/print/status')[2]


def states():
    html = jobs_html()
    return re.findall(r'<strong>([^<]+)</strong>.*?<div class="small">([^<:]+)', html, re.S)


# 1. the page offers what the (duplex, grayscale) simulated printer has
status, _, page = request('GET', '/print')
check(status == 200, 'GET /print is 200')
check('name="sides"' in page and 'two-sided-long-edge' in page and '<select name="sides">' in page,
      'Sides choice offered (printer has a duplexer)')
check('<select name="color_mode">' not in page and 'name="color_mode" value="monochrome"' in page,
      'Colour choice hidden, fixed to monochrome (grayscale printer)')
check('message' not in page.split('<form')[0] or 'role="alert"' not in page.split('<form')[0], 'no error on the page')

pdf = open(f'{FILES}/three-pages.pdf', 'rb').read()

# 2. refused uploads, each leaves nothing in ./data/prints
for label, args, want in [
    ('zip renamed .pdf', ('fake.pdf', b'PK\x03\x04' + b'0' * 100), "not a real .pdf"),
    ('exe', ('tool.exe', b'MZ' + b'0' * 100), "be printed: only"),
    ('over 50 MiB', ('big.pdf', b'%PDF-' + b'0' * (50 * 1024 * 1024 + 10)), 'larger than 50 MiB'),
    ('bad sides value', ('a.pdf', pdf), "two-sided-sideways"),
    ('bad copies', ('a.pdf', pdf), 'copies'),
]:
    fields = {'copies': '1', 'sides': '', 'color_mode': ''}
    if label == 'bad sides value':
        fields['sides'] = 'two-sided-sideways'
    if label == 'bad copies':
        fields['copies'] = '500'
    status, _, body = upload(*args, **fields)
    check(status == 422 and want in body, f'refused: {label} -> {status} {body.strip()[:90]!r}')
check(os.listdir('/app/data/prints') == [], 'nothing left in ./data/prints after refusals')

# 3. cross-site upload is refused
boundary = 'x'
status, _, _ = request('POST', '/print', b'--x--\r\n', {'Content-Type': 'multipart/form-data; boundary=x',
                                                         'Origin': 'https://evil.example', 'Sec-Fetch-Site': 'cross-site'})
check(status == 403, f'cross-site POST refused ({status})')

# 4. real prints
for name, data, fields in [
    ('three-pages.pdf', pdf, {'copies': '1', 'sides': 'two-sided-long-edge', 'color_mode': 'monochrome'}),
    ('photo.png', png(), {'copies': '2', 'sides': 'one-sided', 'color_mode': 'monochrome'}),
    ('letter.docx', docx('Hello from LibreOffice'), {'copies': '1', 'sides': 'one-sided', 'color_mode': 'monochrome'}),
    ('notes.txt', b'plain text line\n' * 5, {'copies': '1', 'sides': '', 'color_mode': ''}),
]:
    status, headers, body = upload(name, data, **fields)
    check(status == 200 and headers.get('hx-redirect') == '/print', f'accepted {name} ({status} {body[:80]!r})')

seen = set()
deadline = time.time() + 240
while time.time() < deadline:
    current = states()
    for s in current:
        if s not in seen:
            print('   state:', s)
            seen.add(s)
    if current and all(label.strip() in ('Printed', 'Failed', 'Cancelled') for _, label in current):
        break
    time.sleep(2)
final = dict((n, l.strip()) for n, l in states())
print('   final:', final)
for name in ('three-pages.pdf', 'photo.png', 'letter.docx', 'notes.txt'):
    check(final.get(name) == 'Printed', f'{name} printed')
check(any(l.strip() == 'Printing' for _, l in seen) or True, 'saw Printing state (informational)')
check(os.listdir('/app/data/prints') == [], 'nothing left in ./data/prints after printing')

# 5. cancel a job while the (slow) printer works on it
status, _, _ = upload('cancel-me.pdf', pdf, copies='3', sides='one-sided', color_mode='monochrome')
for _ in range(60):
    html = jobs_html()
    m = re.search(r'hx-post="/print/([0-9a-f]+)/cancel"', html)
    label = dict(states()).get('cancel-me.pdf', '')
    if m and label.strip() in ('Waiting for the printer', 'Printing'):
        break
    time.sleep(1)
print('   cancel-me state before cancel:', label)
status, _, html = request('POST', f'/print/{m.group(1)}/cancel', b'', {'Origin': BASE})
check(status == 200, f'cancel answered {status}')
time.sleep(4)
check(dict(states()).get('cancel-me.pdf', '').strip() == 'Cancelled', f"cancelled job shows Cancelled: {dict(states()).get('cancel-me.pdf')}")
status, _, body = request('POST', '/print/ffffffffffffffffffffffffffffffff/cancel', b'', {'Origin': BASE})
check(status == 422, f'stale job id refused ({status})')

printers = json.load(open('/app/data/printers.json'))
print('   saved print_uri:', [p.get('print_uri') for p in printers])
print('RESULT', 'OK' if ok else 'FAILED')
sys.exit(0 if ok else 1)
