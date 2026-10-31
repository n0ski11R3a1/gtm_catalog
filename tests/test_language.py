from app import app


def test_language_switch_persists_and_returns_to_requested_page():
    client = app.test_client()

    response = client.get(
        "/language/my?next=%2Fadmin%2Fproducts%3Fstatus%3Din%23top"
    )

    assert response.status_code == 302
    assert response.headers["Location"] == "/admin/products?status=in#top"
    cookie = response.headers["Set-Cookie"]
    assert "gtm_lang=my" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie

    page = client.get("/login")
    assert page.status_code == 200
    assert b'<html lang="my">' in page.data
    assert b'value="my" selected' in page.data
    assert b"language.js" in page.data


def test_language_switch_rejects_external_redirects_and_unknown_languages():
    client = app.test_client()

    external_redirect = client.get(
        "/language/zh?next=https%3A%2F%2Fexample.com%2Fsteal"
    )
    assert external_redirect.status_code == 302
    assert external_redirect.headers["Location"] == "/"
    assert "gtm_lang=zh" in external_redirect.headers["Set-Cookie"]

    unknown_language = client.get("/language/fr?next=%2Flogin")
    assert unknown_language.status_code == 302
    assert unknown_language.headers["Location"] == "/login"
    assert "gtm_lang=en" in unknown_language.headers["Set-Cookie"]


def test_language_selector_renders_on_public_and_admin_surfaces():
    client = app.test_client()

    public_page = client.get("/login")
    assert public_page.status_code == 200
    assert b'class="public-language-control"' in public_page.data
    assert b'value="en" selected' in public_page.data
    assert b'value="my"' in public_page.data
    assert b'value="zh"' in public_page.data

    with client.session_transaction() as session:
        session["admin"] = True
    admin_page = client.get("/admin")
    assert admin_page.status_code == 200
    assert b'class="ui-language-control"' in admin_page.data
    assert b'id="uiLanguage"' in admin_page.data
