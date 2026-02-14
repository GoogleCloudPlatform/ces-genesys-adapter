# Copyright 2025 Google LLC

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     https://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import re
from dotenv import load_dotenv
from google.cloud import secretmanager

load_dotenv()

def resolve_secret(secret_value: str) -> str:
    if not secret_value:
        return secret_value
    if isinstance(secret_value, str) and secret_value.startswith("projects/"):
        secret_path = secret_value
        if "/versions/" not in secret_path:
            secret_path = f"{secret_path}/versions/latest"
        try:
            client = secretmanager.SecretManagerServiceClient()
            response = client.access_secret_version(name=secret_path)
            return response.payload.data.decode("UTF-8").strip()
        except Exception as e:
            print(f"Error fetching secret from Secret Manager: {e}")
            raise
    return secret_value

PORT = os.getenv("PORT", 8080)
GENESYS_API_KEY = resolve_secret(os.getenv("GENESYS_API_KEY"))
AUTH_TOKEN_SECRET_PATH = os.getenv("AUTH_TOKEN_SECRET_PATH")
GENESYS_CLIENT_SECRET = resolve_secret(os.getenv("GENESYS_CLIENT_SECRET"))
LOG_UNREDACTED_DATA = os.getenv("LOG_UNREDACTED_DATA")
DEBUG_WEBSOCKETS = os.getenv("DEBUG_WEBSOCKETS", "false") == 'true'
 