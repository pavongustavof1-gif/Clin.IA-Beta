# backend/tests/test_aviso_beta2.py
# Roadmap #45: the app page shell carries the Beta 2 notice, and its
# dismiss logic is an external same-origin script (the CSP blocks
# un-nonce'd inline scripts). GET / is public and renders no data, so no
# token is needed. The dismiss behavior itself is browser-only JS and
# isn't exercised here (no JS runner in CI yet — roadmap #49).

AVISO_TEXT = (
    'Apreciado usuario: gracias a tu retroalimentación, estamos desarrollando '
    'la siguiente versión, Beta 2. Conforme a los Términos de Servicio, '
    'Clin.IA™ puede presentar cambios, interrupciones o limitaciones.'
)


def test_index_renders_aviso_beta2(client):
    resp = client.get('/')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert AVISO_TEXT in html
    assert 'class="aviso-beta2"' in html
    assert 'aria-label="Cerrar aviso"' in html
    assert '<script src="/static/aviso-beta2.js"></script>' in html


def test_aviso_beta2_script_is_served(client):
    resp = client.get('/static/aviso-beta2.js')
    assert resp.status_code == 200
    assert 'clinia_aviso_beta2_v1' in resp.get_data(as_text=True)
