# ChatGPT: a custom GPT for your database

ChatGPT can't run MCP servers locally, so the GPT calls the Cloud Run API through an Action.

## Set up

1. Deploy the API (see the main README) and note its URL. Set `PUBLIC_URL` on the service
   to that URL so the OpenAPI schema lists it.
2. In ChatGPT, go to **Explore GPTs > Create > Configure**.
3. **Name:** "Thought Leadership". **Instructions:** paste [instructions.md](instructions.md).
4. **Actions > Create new action > Import from URL:** `https://YOUR-SERVICE-URL/openapi.json`.
5. **Authentication:** API Key, Auth Type **Bearer**, and paste your `TLE_API_TOKEN`.
6. **Privacy policy:** your company's policy URL (required to share the GPT).
7. Save it, shared only with your workspace. Anyone using it can read everything in the
   database, private items included, so share it only with people who should.

The Action exposes: `getTaxonomy`, `getPlaybook`, `searchItems`, `getItem`, `getCoverage`,
`getTrends`, `ingestText`, `updateItem` and `addDeliverable`.
