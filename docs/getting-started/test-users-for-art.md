# Test User Details for ART

How to get user details for testing the Azure Real-Time Voice Agent (ART).

---

## Option 1: Create a demo profile (recommended)

Use the **Demo Guide** and create a profile in the UI. The system generates synthetic data (client_id, balance, preferences) for that session.

1. Open the frontend: `http://localhost:5173` or your deployed URL (e.g. `https://artagent-frontend-xxx.azurecontainerapps.io`).
2. Click the **profile icon** (👤) → **Create Demo Profile**.
3. Enter:
   - **Full name** (e.g. `John Smith`)
   - **Email** — use a **real email** if you want to test MFA (verification codes are sent here).
   - **Phone** (optional, E.164 e.g. `+14155551234`) for SMS demos.
   - **Scenario**: `banking` or `insurance`.

**Documentation:** [Demo Guide](demo-guide.md) — Step 1: Create a Demo Profile.

---

## Option 2: Pre-defined mock users (identity verification)

When **verify_client_identity** is used and Cosmos DB is not configured (or as fallback), ART uses built-in mock users. Use **full name** and **SSN last 4** as spoken/typed to “log in” as that user.

| Full name      | SSN last 4 | Client ID       | Email                     |
|----------------|------------|------------------|---------------------------|
| John Smith     | 1234       | CLT-001-JS       | john.smith@email.com      |
| John Smith     | 5678       | john_smith_js    | john.smith.test@example.com |
| Jane Doe       | 5678       | CLT-002-JD       | jane.doe@email.com        |
| Michael Chen   | 9999       | CLT-003-MC       | m.chen@email.com          |
| Alice Brown    | 1234       | alice_brown_ab   | alice.brown@example.com   |
| Bob Williams   | 5432       | bob_williams_bw  | bob.williams@example.com  |
| Sarah Johnson  | 4321       | sarah_johnson_sj | sarah.johnson@example.com |

**Example:** Say *“My name is John Smith and my SSN last four is 1-2-3-4”* to be recognized as John Smith (CLT-001-JS).

**Source (for developers):** `apps/artagent/backend/registries/toolstore/auth.py` — `_MOCK_USERS`.

---

## Option 3: Insurance test scenarios

For the **insurance** scenario, predefined claim scenarios are available via the **test_scenario** parameter when creating a demo profile (e.g. as CC rep). Examples: `golden_path`, `demand_under_review`, `coverage_denied`, etc.

**Documentation:** [Demo Guide – Create Demo Profile](demo-guide.md) (insurance fields and test_scenario).  
**Source:** `apps/artagent/backend/api/v1/endpoints/demo_env.py` — `DemoUserRequest.test_scenario` and insurance constants.

---

## Quick reference

| Need                    | Where to go / what to use                    |
|-------------------------|----------------------------------------------|
| Full demo walkthrough   | [Demo Guide](demo-guide.md)                  |
| Create profile in UI    | Frontend → 👤 → Create Demo Profile          |
| Pre-defined test users  | Table above (name + SSN last 4)              |
| MFA testing            | Use your real email when creating a profile |
| Insurance test claims   | Create profile with scenario=insurance, set test_scenario (e.g. golden_path) |
