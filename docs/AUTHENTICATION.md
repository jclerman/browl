# Setting up authentication

browl's admin pages need Google sign-in ([`DESIGN.md`](DESIGN.md) §2.11). Each operator registers their own OAuth
client with Google, once, and gives browl its ID and secret.

## Projects, clients and deployments

- **Every operator needs their own client, in a Google Cloud project they control.** It can be a new project, or one
  they or their organization already has. browl can't ship a client: Google sends users back only to the addresses
  registered on it, and its secret must stay secret. Nor should you use someone else's client: its owner would control
  your sign-in, your users would see the owner's app name on the consent screen, and you would share the owner's
  test-user list.
- **One project can serve all of an operator's deployments**, such as a local one and production. Give each deployment
  its own client in that project. One client can list several redirect URIs, but with separate clients, a leaked
  development secret can't be used for production, and either can be rotated or deleted without breaking the other.
- **The consent screen belongs to the project, so all its clients share it**: app name, audience, publishing status,
  and in Testing, the test-user list (below). Each client has its own ID, secret and redirect URIs.
- **Admins are listed in each deployment, not at Google.** Google only confirms who someone is. Each deployment's
  admins are its own `SEED_ADMINS` and the admins added in its UI, kept in its `DATA_DIR`. So deployments sharing a
  project, or even a client, can have entirely different admins.

For development without signing in, set `AUTH_DISABLED=true` instead (`.env.example` does). Everyone is then an admin.
Never set it in production.

## At Google

In the [Google Cloud console](https://console.cloud.google.com/), no billing or API needs to be enabled.

1. **Create a project**, or pick one of yours.
2. **Configure the consent screen** in the Google Auth Platform: from the project's welcome page, APIs & Services →
   OAuth consent screen, or search the console for "Google Auth Platform". In a new project, **Get started** opens a
   wizard that asks for the app information, audience and contact information; later, the platform's menu has them
   under Branding and Audience.
   - Give an app name and a support email address. Users see them when they sign in.
   - Leave out the logo: adding one needs Google to verify the app's brand.
   - Audience: **External**, unless every admin is in your Google Workspace organization, when **Internal** limits
     sign-in to it.
   - Publishing status (under Audience, with the test users): in **Testing**, only the test users you list (up to 100)
     can sign in, to any of the project's clients. Publish the app (**In production**) to drop that list: any
     Google account can then sign in, and each deployment refuses the admin pages to those who aren't its admins.
     browl asks only for the `openid` and `email` scopes, which need no review; leave Data Access empty.
3. **Create a client for each deployment** (Clients → Create client → **Web application**). Under **Authorized
   redirect URIs**, add the deployment's callback: `BASE_URL` without its trailing `/`, then
   `/{RESERVED_PREFIX}/admin/oidc-callback`. For example `https://ontology.example.org/_browl/admin/oidc-callback`,
   or for a local deployment on the default port, `http://localhost:8000/_browl/admin/oidc-callback`. The URI must
   match exactly. Google accepts `http` only for `localhost`, and no bare IP addresses, so a deployment elsewhere
   needs a domain name and HTTPS, with `BASE_URL` set to its `https` address. If you change `BASE_URL` or
   `RESERVED_PREFIX`, register the new URI.
4. **Copy the client ID and secret.**

## In browl

Set these in `.env` (or the app's environment), and leave `AUTH_DISABLED` unset or `false`:

```sh
OIDC_CLIENT_ID=1234567890-abc.apps.googleusercontent.com
OIDC_CLIENT_SECRET=GOCSPX-…
SEED_ADMINS='["you@example.org"]'
```

`SEED_ADMINS` lists the admins' Google account email addresses, as JSON. They can't be removed in the UI. Only
accounts whose address Google reports as verified can sign in.

Sign in from the "Sign in" link in the navigation bar. A signed-in user who isn't an admin is refused the admin pages.
Sign-ins last 12 hours. Signing out ends the account's sign-ins on every device. Session cookies are signed with a key
browl makes on first start and keeps in `DATA_DIR/session.key`. Deleting that file and restarting signs everyone out.
If the file is ever emptied or cut short, browl refuses to start, saying so; delete it, and browl makes a new key.
