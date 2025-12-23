#!/bin/bash

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

source $(dirname "$0")/values.sh

gcloud run deploy $SERVICE_NAME \
    --source="." \
    --platform=managed \
    --region=$LOCATION \
    --cpu=1 \
    --memory=1Gi \
    --min-instances=1 \
    --max-instances=10 \
    --service-account=$SERVICE_ACCOUNT \
    --allow-unauthenticated \
    --project=$PROJECT_ID  \
    --timeout=$TIMEOUT \
    --concurrency=$CONCURRENCY \
    --set-env-vars=AUTH_TOKEN_SECRET_PATH="$AUTH_TOKEN_SECRET_PATH",NUMBERS_COLLECTION_ID="$NUMBERS_COLLECTION_ID",LOG_UNREDACTED_DATA="$LOG_UNREDACTED_DATA" \
    --set-secrets=GENESYS_API_KEY="$GENESYS_API_KEY_SECRET_PATH",GENESYS_CLIENT_SECRET="$GENESYS_CLIENT_SECRET_PATH" \
    --allow-unauthenticated \
    --startup-probe=httpGet.path=/health,httpGet.port=8080
