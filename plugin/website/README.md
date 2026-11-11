# Files the website serves for the plugin

`oauth/memorysafe-plugin.json` is the plugin's OAuth client identity document (CIMD). Its URL
is the plugin's `client_id` (`client_identity.CLIENT_ID`); the hosted sign-in server fetches
it over https when a computer signs in, checks that the document names that same URL, and
takes the plugin's name and loopback redirect URIs from it. No real sign-in works until the
website serves it.

`_headers` is Netlify's header file for it: JSON content type, an hour of cache (the server
caps at 24), and open CORS.

Both are generated from `client_identity.client_document()`:

    .venv/bin/python -c "import json; from memorysafe_chatgpt import client_identity; print(json.dumps(client_identity.client_document(), indent=2, sort_keys=True))"

`tests/test_device_link.py` pins the committed file to that function, so the two cannot
drift. Copy both files into the website's `public/` as they are (the website source lives
outside this repository); the copy is what gets deployed, and deploying is the owner's call.
