"""Host-to-Runtime fixed origin migration; never emit token/response bodies."""
import json
import os
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def main():
    env = os.environ
    url = "http://" + env["AI_SERVICE_BIND_ADDRESS"] + ":" + env["AI_SERVICE_HTTP_PORT"]
    endpoint = {"alias": "website-alias", "retire": "website-retire"}.get(env.get("WEBSITE_ACTION"), "website-origin")
    request = urllib.request.Request(url + "/management/" + endpoint, method="POST",
        data=json.dumps({"old_url": "https://" + env["WEBSITE_OLD_DOMAIN"],
                         "new_url": "https://" + env["WEBSITE_NEW_DOMAIN"]}).encode(),
        headers={"Authorization": "Bearer " + env["SERVICE_MANAGEMENT_TOKEN"], "Content-Type": "application/json"})
    with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(request, timeout=120) as response:
        if response.status != 200:
            raise ValueError


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("website_origin_update_failed") from None
