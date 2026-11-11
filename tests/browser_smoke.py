"""Run with Playwright against an isolated local test instance, never the owner's port."""

import os
import sys
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, expect


def save_form(page, form, name):
    with page.expect_navigation(wait_until="load"):
        form.get_by_role("button", name=name, exact=True).click()


def check_controls(page, url):
    # Hover, pointer transfer, persistent click, keyboard and outside dismissal.
    button = page.locator("[data-help]").first
    button.hover()
    tip = page.locator("#" + button.get_attribute("aria-describedby"))
    tip.wait_for(state="visible")
    tip.hover()
    page.wait_for_timeout(450)
    assert tip.is_visible()
    button.click()
    page.locator("h1").hover()
    page.wait_for_timeout(450)
    assert tip.is_visible()
    page.keyboard.press("Escape")
    assert not tip.is_visible()
    button.focus()
    page.keyboard.press("Enter")
    assert tip.is_visible()
    page.keyboard.press("Tab")
    assert tip.is_visible()  # pinned, without a focus trap
    page.locator("h1").click()
    assert not tip.is_visible()
    button.focus()
    page.keyboard.press("Space")
    assert tip.is_visible()
    page.keyboard.press("Escape")
    # Help in a form must never submit. Check its position at page edges and zoom.
    form = page.locator("form").filter(has=page.locator("[name=interval]"))
    before = page.url
    help_button = form.locator("[data-help]").last
    help_button.click()
    page.wait_for_timeout(200)
    assert page.url == before and form.locator("[name=interval]").input_value() == "5"
    page.evaluate("document.body.style.zoom='1.5'")
    page.wait_for_timeout(100)
    box = page.locator("#" + help_button.get_attribute("aria-describedby")).bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= page.viewport_size["width"] + 1
    page.keyboard.press("Escape")
    page.evaluate("document.body.style.zoom='1'")
    # No network collection: manual is saved atomically with public selection.
    form.locator("[name=collection_auto]").select_option("false")
    form.locator("[name=source]").select_option("public")
    form.locator("[name=period]").fill("2026.2")
    save_form(page, form, "Salvar configuração")
    page.wait_for_url(url + "/?saved=1#settings")
    assert page.locator("[name=source]").input_value() == "public"
    form = page.locator("form").filter(has=page.locator("[name=interval]"))
    form.locator("[name=source]").select_option("demo")
    form.locator("[name=period]").fill("DEMO.1")
    save_form(page, form, "Salvar configuração")
    # Agenda local input is interpreted in Brasilia, even with a Tokyo browser.
    schedule = page.locator("form.schedule").first
    schedule.locator("..").locator("summary").click(position={"x": 8, "y": 8})
    schedule.locator("select").select_option("true")
    schedule.locator("[type=datetime-local]").first.fill("2026-11-12T08:00")
    schedule.locator("[type=time]").nth(0).fill("22:00")
    schedule.locator("[type=time]").nth(1).fill("02:00")
    schedule.locator("[type=checkbox]").first.check()
    assert "dia seguinte" in schedule.locator(".schedule-preview").inner_text()
    save_form(page, schedule, "Salvar agenda")
    schedule = page.locator("form.schedule").first
    schedule.locator("..").locator("summary").click(position={"x": 8, "y": 8})
    assert schedule.locator("[type=datetime-local]").first.input_value() == "2026-11-12T08:00"
    page.set_viewport_size({"width": 390, "height": 844})
    help_button = page.locator("[data-help]").last
    help_button.click()
    tip = page.locator("#" + help_button.get_attribute("aria-describedby"))
    assert tip.is_visible()
    box = tip.bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= 391
    page.keyboard.press("Escape")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth+1")
    page.set_viewport_size({"width": 1440, "height": 1000})
    # Touch has the same persistent help behavior and no form side effects.
    touch = page.context.browser.new_page(
        viewport={"width": 390, "height": 844},
        has_touch=True,
        is_mobile=True,
        timezone_id="America/New_York",
    )
    touch.goto(url)
    touch.locator("[name=password]").fill(os.environ["VAGAS_TEST_PASSWORD"])
    touch.get_by_role("button", name="Entrar no painel").click()
    touch.wait_for_url(url + "/")
    trigger = touch.locator("[data-help]").first
    trigger.tap()
    popup = touch.locator("#" + trigger.get_attribute("aria-describedby"))
    assert popup.is_visible()
    touch.wait_for_timeout(400)
    assert popup.is_visible()
    trigger.tap()
    assert not popup.is_visible()
    touch.close()
    # Two tabs cannot silently replace one another's configuration.
    other = page.context.new_page()
    other.goto(url)
    other.locator("[name=interval]").fill("6")
    save_form(
        other,
        other.locator("form").filter(has=other.locator("[name=interval]")),
        "Salvar configuração",
    )
    page.locator("[name=interval]").fill("7")
    page.get_by_role("button", name="Salvar configuração", exact=True).click()
    expect(page.locator("#save-feedback")).to_contain_text("Outra aba")
    assert page.locator("[name=interval]").input_value() == "7"
    other.close()
    page.reload()
    page.locator("[name=interval]").fill("5")
    save_form(
        page,
        page.locator("form").filter(has=page.locator("[name=interval]")),
        "Salvar configuração",
    )


def main():
    url = os.environ["VAGAS_TEST_BROWSER_URL"]
    parsed = urlsplit(url)
    assert parsed.hostname == "127.0.0.1" and parsed.port not in {8765, 8766}
    password = os.environ["VAGAS_TEST_PASSWORD"]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(
            viewport={"width": 1440, "height": 1000}, timezone_id="Asia/Tokyo"
        )
        page = context.new_page()
        origins = []
        page.on(
            "request",
            lambda r: origins.append(r.headers.get("origin")) if r.method == "POST" else None,
        )
        page.goto(url)
        if "--existing" not in sys.argv:
            assert page.url.endswith("/setup")
            page.locator("[name=password]").fill(password)
            page.locator("[name=confirm]").fill(password + "wrong")
            page.get_by_role("button", name="Cadastrar senha").click()
            assert page.locator("[role=alert]").is_visible()
            page.locator("[name=password]").fill(password)
            page.locator("[name=confirm]").fill(password)
            page.get_by_role("button", name="Cadastrar senha").click()
            page.wait_for_url(url + "/login")
            page.locator("[name=password]").fill("wrong")
            page.get_by_role("button", name="Entrar no painel").click()
            assert page.locator("[role=alert]").is_visible()
        else:
            assert page.url.endswith("/login")
        page.locator("[name=password]").fill(password)
        page.get_by_role("button", name="Entrar no painel").click()
        page.wait_for_url(url + "/")
        assert page.get_by_role("heading", name="Visão geral", exact=True).is_visible()
        assert all(origin == url for origin in origins)
        assert page.locator("#live-status").inner_text() != ""
        page.locator("[name=interval]").fill("5")
        save_form(
            page,
            page.locator("form").filter(has=page.locator("[name=interval]")),
            "Salvar configuração",
        )
        page.wait_for_url(url + "/?saved=1#settings")
        assert page.locator("[name=interval]").input_value() == "5"
        check_controls(page, url)
        screenshot = os.environ.get("VAGAS_TEST_SCREENSHOT")
        if screenshot:
            page.locator("#overview").scroll_into_view_if_needed()
            page.screenshot(path=screenshot, full_page=False)
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth+1")
        if "--existing" not in sys.argv:
            form = page.locator('form[action="/password"]')
            form.locator("[name=current]").fill(password)
            form.locator("[name=password]").fill(password + "-new")
            form.locator("[name=confirm]").fill(password + "-new")
            form.get_by_role("button", name="Trocar senha e encerrar sessões", exact=True).click()
            page.wait_for_url(url + "/login")
        browser.close()
    print("Chromium form/Origin, bootstrap, login errors, settings and responsive layout passed")


if __name__ == "__main__":
    main()
