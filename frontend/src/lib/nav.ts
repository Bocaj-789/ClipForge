/** Full-page navigation (e.g. to Google's consent screen). Isolated so tests can replace it. */
export function goTo(url: string): void {
  window.location.assign(url);
}
