import { BRAND_SVG } from './site-header.js';

// Shared site footer. Each page picks its own nav entries, but the brand
// mark, the wordmark, and the `.foot-note` come from here so we're not
// copying 30 lines of SVG into every HTML file.

function linkHTML({ href, label }) {
  return `<a href="${href}">${label}</a>`;
}

export function siteFooterHTML(links = []) {
  const nav = links.map(linkHTML).join('');
  return `
    <div class="foot-inner">
      <span class="brand">
        <span>fathom</span>
        ${BRAND_SVG}
      </span>
      <nav class="foot-nav">${nav}</nav>
      <span class="foot-note mono">fathomdx.io</span>
    </div>
  `;
}

export function mountSiteFooter(links = []) {
  const el = document.querySelector('[data-site-footer]');
  if (!el) return;
  if (!el.classList.contains('foot')) el.classList.add('foot');
  el.innerHTML = siteFooterHTML(links);
}
