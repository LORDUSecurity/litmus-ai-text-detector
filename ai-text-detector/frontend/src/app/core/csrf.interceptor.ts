import { HttpInterceptorFn } from '@angular/common/http';

/** The name the backend sets, the header it reads, and the safe methods it skips.
 *  Mirrors `backend/security.py` -- if either side drifts, signed-in writes 403. */
export const CSRF_COOKIE = 'detector_csrf';
export const CSRF_HEADER = 'X-CSRF-Token';
const SAFE = new Set(['GET', 'HEAD', 'OPTIONS']);

/** The session's CSRF token, or '' when no session has been issued one yet.
 *
 *  Read from `document.cookie` rather than kept in a service because the value is the server's:
 *  it is minted on the first authenticated read, replaced when the owner signs out and signs in as
 *  somebody else, and deleted by logout. A copy taken at boot would go stale on exactly the two
 *  transitions that must not break -- and a stale token 403s every later write with a message the
 *  user cannot act on. */
export function csrfToken(doc: Document = document): string {
  for (const pair of doc.cookie.split(';')) {
    const [name, ...rest] = pair.trim().split('=');
    if (name === CSRF_COOKIE) return decodeURIComponent(rest.join('='));
  }
  return '';
}

/** Puts the session token on every state-changing call, which is what lets the backend tell
 *  "this came from our own page" from "a form some other site posted with your cookie".
 *
 *  GETs are left alone: the guard only asks on writes, and a read is how the token arrives in the
 *  first place. A write with no token yet (the sign-in and sign-up calls themselves, and every
 *  anonymous probe) is sent as-is rather than with an empty header -- an empty value is a wrong
 *  answer where sending nothing is simply not an answer. */
export const csrfInterceptor: HttpInterceptorFn = (req, next) => {
  if (SAFE.has(req.method.toUpperCase())) return next(req);
  const token = csrfToken();
  if (!token) return next(req);
  return next(req.clone({ setHeaders: { [CSRF_HEADER]: token } }));
};
