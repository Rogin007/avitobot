"""HTML parsing and Ekb -> RF fallback URL derivation."""
import pytest

from avitobot.parser import parse_listings, make_rf_url, _looks_like_captcha


def _item(title, price, item_id, location="Екатеринбург"):
    return f"""
    <div data-marker="item">
      <div data-marker="item-specific-params">
        <ul>
          <li>Состояние: Б/У</li>
          <li>Год выпуска: 2021</li>
        </ul>
      </div>
      <a itemprop="url" data-marker="item-title" href="/ekaterinburg/t/test_{item_id}">
        <h3 itemprop="name" data-marker="item-title">{title}</h3>
      </a>
      <meta itemprop="price" content="{price}">
      <p data-marker="item-description">Описание лота</p>
      <span data-marker="item-address">{location}</span>
      <div data-marker="item-date">Сегодня 12:00</div>
    </div>
    """


HTML = "<html><body>" + "".join([
    _item("PlayStation 4 Pro 1TB", 12000, "111"),
    _item("ps4 slim 500 гб 2 геймпада", 8000, "222"),
    _item("RTX 3060 12GB", 18000, "333"),
]) + "</body></html>"


class TestParseListings:
    def test_extracts_fields(self):
        lots = parse_listings(HTML, "https://www.avito.ru", "local")
        assert len(lots) == 3
        first = lots[0]
        assert first.id == "111"
        assert first.price == 12000
        assert "PlayStation" in first.title
        assert first.url.endswith("test_111")
        assert first.location == "Екатеринбург"
        assert first.scope == "local"
        assert first.params["Состояние"] == "Б/У"

    def test_absolute_url(self):
        lots = parse_listings(HTML, "https://www.avito.ru", "local")
        assert lots[0].url.startswith("https://www.avito.ru/")

    def test_id_fallback_to_title(self):
        html = '<div data-marker="item"><a itemprop="url" href="/x">Нет id</a></div>'
        lots = parse_listings(html, "https://www.avito.ru", "local")
        assert lots  # degrades gracefully


class TestCaptcha:
    def test_captcha_detection(self):
        assert _looks_like_captcha(b"Please verify you are a human captcha")
        assert not _looks_like_captcha(b"normal page content")


class TestRfUrl:
    def test_strips_city_segment(self):
        url = "https://www.avito.ru/ekaterinburg/tovary_dlya_kompyutera/videokarty?q=rtx"
        assert "ekaterinburg" not in make_rf_url(url)
        assert make_rf_url(url).startswith("https://www.avito.ru/tovary")

    def test_keeps_non_city(self):
        url = "https://www.avito.ru/videokarty?q=rtx"
        assert make_rf_url(url) == url

    def test_drops_location_params(self):
        url = "https://www.avito.ru/tovary?q=rtx&location=621540"
        assert "location" not in make_rf_url(url)