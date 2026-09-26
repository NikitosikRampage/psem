"""Очистка HTML страницы перед сохранением: без скриптов, токенов, телефонов и e-mail."""
from __future__ import annotations

import re

# Чистка DOM-клона внутри страницы: без скриптов, стилей, скрытых полей и токенов.
SANITIZE_JS = r"""
() => {
  const doc = document.documentElement.cloneNode(true);
  doc.querySelectorAll('script, noscript, style, iframe, svg, link[rel="preload"], link[rel="prefetch"]')
     .forEach(e => e.remove());
  doc.querySelectorAll('meta').forEach(m => {
    const n = (m.getAttribute('name') || m.getAttribute('property') || '').toLowerCase();
    if (/csrf|token|verification|session/.test(n)) m.remove();
  });
  doc.querySelectorAll('input').forEach(i => {
    if ((i.getAttribute('type') || '').toLowerCase() === 'hidden' || /token|csrf/i.test(i.name || ''))
      i.setAttribute('value', '');
  });
  doc.querySelectorAll('*').forEach(e => {
    for (const a of Array.from(e.attributes)) {
      if (a.name === 'style' || a.name.startsWith('on')) e.removeAttribute(a.name);
      else if (a.name === 'src' && a.value.startsWith('data:')) e.setAttribute('src', 'data:');
      else if (a.name === 'srcset') e.removeAttribute(a.name);
    }
  });
  return '<!doctype html>\n' + doc.outerHTML;
}
"""

_PHONE_RE = re.compile(r"(?<!\d)(?:\+7|8)[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

_ORDER_LINK_FALLBACK = "a[href*='o.php'], a[href*='/order'], a[href*='order_id'], a[href*='orderId']"


def scrub(html: str) -> str:
    html = _PHONE_RE.sub("+7 000 000-00-00", html)
    return _EMAIL_RE.sub("user@example.com", html)


def sanitized_html(page) -> str:
    """Очищенный HTML текущей страницы Playwright."""
    try:
        return scrub(page.evaluate(SANITIZE_JS))
    except Exception as exc:  # noqa: BLE001
        return f"<!-- не удалось очистить DOM: {exc} -->"
