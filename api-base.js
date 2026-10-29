/* ===========================================================================
   WHERE THE API LIVES  -  the one setting to edit after deploying.

   Both pages (index.html, landing.html) read window.INVIGISMART_API before
   building any request URL, so this file is the whole configuration surface.

     '' (blank)  -> same origin. Correct whenever the Python server serves the
                    pages itself: http://localhost:8100 locally, or the VPS /
                    PaaS host that runs server/app.py.

     'https://...' -> the API's origin, no trailing slash, when the pages are
                    uploaded to the cPanel host (invigismart.online) and the
                    backend runs somewhere else. Example:

                      window.INVIGISMART_API = 'https://api.invigismart.online';

   The backend answers CORS with Access-Control-Allow-Origin: * and accepts the
   Authorization header, so a cross-origin call needs nothing set server-side.
   Sessions travel as a bearer token, not a cookie, which is why "*" is safe
   here: no ambient credentials are sent to a third-party origin.

   Because this is a script the pages can neither cache-bust nor skip, a stale
   copy is the usual reason "the site still cannot reach the API" after an
   upload - reload with the cache disabled (Ctrl+F5) before debugging further.
   =========================================================================== */
window.INVIGISMART_API = '';
