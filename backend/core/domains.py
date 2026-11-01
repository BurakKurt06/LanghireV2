"""A host's site ("registrable domain") without downloading the public suffix list.

'careers.acme.co.uk' -> 'acme.co.uk', 'jobs.acme.com' -> 'acme.com', and on hosts
where anyone can get a name, each name is its own site: 'alice.github.io' stays
'alice.github.io' (a plugin for it must not get every *.github.io page).
"""

# Hosting under which anyone can register a name (a short list of the common ones).
SHARED_SUFFIXES = frozenset({
    "github.io", "gitlab.io", "githubusercontent.com", "github.dev", "netlify.app", "vercel.app",
    "pages.dev", "workers.dev", "web.app", "firebaseapp.com", "herokuapp.com", "azurewebsites.net",
    "cloudfront.net", "appspot.com", "blogspot.com", "wordpress.com", "wixsite.com", "squarespace.com",
    "myshopify.com", "onrender.com", "fly.dev", "glitch.me", "repl.co", "replit.app", "notion.site",
    "s3.amazonaws.com", "amazonaws.com", "ngrok.io", "ngrok-free.app", "trycloudflare.com",
    "gitbook.io", "readthedocs.io", "webflow.io", "framer.website", "carrd.co", "weebly.com",
})
# Second levels of country domains: 'acme.co.uk', 'acme.com.au', 'acme.ac.jp'.
_COUNTRY_SECOND_LEVELS = frozenset({"co", "com", "ac", "org", "net", "gov", "edu", "ne", "or", "go", "gob", "ltd", "plc"})


def registrable(host: str) -> str:
    labels = [p for p in (host or "").lower().strip(".").split(".") if p]
    for size in (3, 2):
        if len(labels) > size and ".".join(labels[-size:]) in SHARED_SUFFIXES:
            return ".".join(labels[-size - 1:])
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _COUNTRY_SECOND_LEVELS:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])
