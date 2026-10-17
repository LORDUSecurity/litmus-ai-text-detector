/** How long the browser will wait for one request before giving up on it.
 *
 *  The browser is the outermost tier of a chain that has to nest, and until now it was the
 *  tier with the *smallest* number in it:
 *
 *      one lane call          240 s  (the shipped primary's ceiling)
 *      the backend's wait     270 s  (lane + 30 s of grace, app.py)
 *      a whole request        250 s  (REQUEST_BUDGET_S, app.py -- the budget a failover's
 *                                     second call has to share; app.py takes the max of this
 *                                     and the lane's own wait, so 270 s is the real ceiling)
 *      gunicorn               285 s  (docker/backend-entrypoint.sh)
 *      nginx                  330 s  (docker/nginx.conf proxy_read_timeout)
 *      the browser            150 s for /detect, 60 s for a key probe
 *
 *  An inner tier that gives up first turns a request the server is still honouring into a
 *  failure the user can't explain: the detection carried on behind the abort and landed in
 *  History while the panel said it failed, and a key on a congested pool was reported dead at
 *  60 s -- the exact false negative that `probe_timeout_s` was written to remove.
 *
 *  360 s clears nginx's 330 s, so the answer the user gets is the one the server decided on
 *  (a verdict, a named ENGINE_TIMEOUT, a gateway 504) rather than a client-side abort, and a
 *  timeout here means what a timeout should mean: the browser itself is stuck. Nothing else in
 *  this file's consumers waits 360 s on purpose -- a healthy gateway replies in a couple of
 *  seconds and the number is only ever reached when something upstream is genuinely slow.
 *
 *  `tools/verify_deploy_recipe.py` fails if any tier above this grows past it.
 */
export const HTTP_PATIENCE_MS = 360_000;
