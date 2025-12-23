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

from dotenv import load_dotenv

load_dotenv()

PORT = os.getenv("PORT", 8080)
GENESYS_API_KEY = os.getenv("GENESYS_API_KEY")
AUTH_TOKEN_SECRET_PATH = os.getenv("AUTH_TOKEN_SECRET_PATH")
GENESYS_CLIENT_SECRET = os.getenv("GENESYS_CLIENT_SECRET")
LOG_UNREDACTED_DATA = os.getenv("LOG_UNREDACTED_DATA")
