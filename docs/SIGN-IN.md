# Setting up Google sign-in

browl's admin pages need Google sign-in ([`DESIGN.md`](DESIGN.md) §2.11). Each operator registers their own OAuth
client with Google, once, and gives browl its ID and secret. browl can't ship one: Google sends users back only to the
addresses registered on the client, and the client's secret must stay secret. One client can serve several deployments,
such as production and a local one, by registering each one's redirect URI. Separate clients are safer, though: a
leaked development secret then can't be used for production.

For development without signing in, set `AUTH_DISABLED=true` instead (`.env.example` does). Everyone is then an admin.
Never set it in production.

## At Google

In the [Google Cloud console](https://console.cloud.google.com/), no billing or API needs to be enabled.

1. **Create a project**, or pick one.
2. **Configure the consent screen** (Google Auth Platform → Branding, then Audience):
   - Give an app name and a support email address. Users see them when they sign in.
   - Leave out the logo: adding one needs Google to verify the app's brand.
   - Audience: **External**, unless every admin is in your Google Workspace organization, when **Internal** limits
     sign-in to it.
   - Publishing status: in **Testing**, only the test users you list (up to 100) can sign in. Publish the app (**In
     production**) to drop that list. browl asks only for the `openid` and `email` scopes, which need no review.
3. **Create the client** (Clients → Create client → **Web application**). Under **Authorized redirect URIs**, add the
   callback of each deployment it serves: `BASE_URL` without its trailing `/`, then
   `/{RESERVED_PREFIX}/admin/oidc-callback`. For example `https://ontology.example.org/_browl/admin/oidc-callback`,
   or for a local deployment on the default port, `http://localhost:8000/_browl/admin/oidc-callback`. The URI must
   match exactly. Google accepts `http` only for
   `localhost`, and no bare IP addresses. If you change `BASE_URL` or `RESERVED_PREFIX`, register the new URI.
4. **Copy the client ID and secret.**

## In browl

Set these in `.env` (or the app's environment), and leave `AUTH_DISABLED` unset or `false`:

```sh
OIDC_CLIENT_ID=1234567890-abc.apps.googleusercontent.com
OIDC_CLIENT_SECRET=GOCSPX-…
SEED_ADMINS='["you@example.org"]'
```

`SEED_ADMINS` lists the admins' Google account email addresses, as JSON. They can't be removed in the UI. Only
addresses Google reports as verified sign in.

Sign in from the "Sign in" link in the navigation bar. A signed-in user who isn't an admin is refused the admin pages.
Sign-ins last 12 hours. They're signed by a key browl makes on first start and keeps in `DATA_DIR/session.key`.
Deleting that file and restarting signs everyone out.
