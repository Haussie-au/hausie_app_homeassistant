import requests


class CloudClient:
    def __init__(self, base_url: str, token: str | None = None, timeout_s: int = 20) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.headers = {"Content-Type": "application/json"}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    def request_base_assets(self, payload: dict) -> dict:
        url = f"{self.base_url}/api/addon/base-assets"
        body = dict(payload or {})
        body.setdefault("force_full", True)
        resp = requests.post(url, headers=self.headers, json=body, timeout=self.timeout_s)
        if resp.status_code // 100 != 2:
            raise RuntimeError(f"Cloud base-assets failed {resp.status_code}: {resp.text}")
        return resp.json() if resp.content else {}

    def request_create_hausie(self, payload: dict) -> dict:
        url = f"{self.base_url}/api/addon/create-hausie"
        body = dict(payload or {})
        resp = requests.post(url, headers=self.headers, json=body, timeout=self.timeout_s)
        if resp.status_code // 100 != 2:
            raise RuntimeError(f"Cloud create-hausie failed {resp.status_code}: {resp.text}")
        return resp.json() if resp.content else {}

    def request_test_assets(self, payload: dict) -> dict:
        url = f"{self.base_url}/api/addon/test-assets"
        body = dict(payload or {})
        body.setdefault("force_full", True)
        resp = requests.post(url, headers=self.headers, json=body, timeout=self.timeout_s)
        if resp.status_code // 100 != 2:
            raise RuntimeError(f"Cloud test-assets failed {resp.status_code}: {resp.text}")
        return resp.json() if resp.content else {}

    def request_rebuild_plan(self, payload: dict) -> dict:
        url = f"{self.base_url}/api/addon/rebuild-plan"
        body = dict(payload or {})
        resp = requests.post(url, headers=self.headers, json=body, timeout=self.timeout_s)
        if resp.status_code // 100 != 2:
            raise RuntimeError(f"Cloud rebuild-plan failed {resp.status_code}: {resp.text}")
        return resp.json() if resp.content else {}

    def register_device(self, payload: dict) -> dict:
        url = f"{self.base_url}/api/device/register"
        body = dict(payload or {})
        resp = requests.post(url, headers=self.headers, json=body, timeout=self.timeout_s)
        if resp.status_code // 100 != 2:
            raise RuntimeError(f"Cloud device register failed {resp.status_code}: {resp.text}")
        return resp.json() if resp.content else {}

    def request_subscription_status(self) -> dict:
        url = f"{self.base_url}/api/device/subscription-status"
        resp = requests.get(url, headers=self.headers, timeout=self.timeout_s)
        if resp.status_code // 100 != 2:
            raise RuntimeError(f"Cloud subscription-status failed {resp.status_code}: {resp.text}")
        return resp.json() if resp.content else {}

    def has_valid_device_credentials(self) -> bool:
        url = f"{self.base_url}/api/device/subscription-status"
        resp = requests.get(url, headers=self.headers, timeout=self.timeout_s)
        if resp.status_code // 100 == 2:
            return True
        if resp.status_code in {401, 404}:
            return False
        raise RuntimeError(f"Cloud subscription-status failed {resp.status_code}: {resp.text}")
