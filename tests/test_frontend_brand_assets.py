from pathlib import Path
import re


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ROOT = PROJECT_ROOT / "frontend"
ASSET_ROOT = FRONTEND_ROOT / "assets" / "royal-tiger"


def test_brand_mark_slots_use_supplied_royal_tiger_assets() -> None:
    html = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")
    app_js = (FRONTEND_ROOT / "app.js").read_text(encoding="utf-8")
    css = (FRONTEND_ROOT / "styles.css").read_text(encoding="utf-8")

    image_sources = re.findall(r'<img[^>]+src="([^"]+)"', html)
    assert image_sources
    assert all(source.startswith("/static/assets/royal-tiger/") for source in image_sources)
    assert "/static/assets/royal-tiger/royal-tiger-logo-banner.png" in image_sources
    assert "/static/assets/royal-tiger/tiger-wave.png" in image_sources
    assert "/static/assets/royal-tiger/tiger-avatar.jpg" in image_sources
    assert 'href="/static/assets/royal-tiger/tiger-avatar.jpg"' in html
    assert "TIGER_AVATAR_SRC" in app_js
    assert "welcome-brand-avatar" in html
    assert "皇家小虎取数 Agent" in html
    assert "把取数问题交给皇家小虎" in html
    assert "welcome-symbol" not in html + css
    assert "brand-mark" not in html + css

    for filename in (
        "royal-tiger-logo-banner.png",
        "tiger-wave.png",
        "tiger-avatar.jpg",
    ):
        assert (ASSET_ROOT / filename).is_file()
