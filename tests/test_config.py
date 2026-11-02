"""Config parsing: proxy, headers."""
from avitobot.config import parse_proxy, Config, EKATERINBURG_LOCATION


def test_parse_proxy_http():
    p = parse_proxy("http://user:pass@1.2.3.4:8080")
    assert p["http"] == "http://user:pass@1.2.3.4:8080"
    assert p["https"] == p["http"]


def test_parse_proxy_socks5():
    p = parse_proxy("socks5://127.0.0.1:1080")
    assert p and "socks5" in p["http"]


def test_parse_proxy_mixed():
    p = parse_proxy("http=http://a:8080,socks5=socks5://b:1080")
    assert p["http"] == "http://a:8080"
    assert p["socks5"] == "socks5://b:1080"


def test_parse_proxy_none():
    assert parse_proxy(None) is None
    assert parse_proxy("") is None


def test_default_headers_chrome_like():
    cfg = Config()
    h = cfg.request_headers
    assert "ru-RU" in h["Accept-Language"]
    assert "Sec-Ch-Ua" in h
    assert "Chrome/124" in h["User-Agent"]


def test_location_iconstant():
    assert EKATERINBURG_LOCATION == 621540