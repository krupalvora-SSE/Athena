# Athena — Sample Questions Reference

Categorized list of questions Athena can handle, based on real user queries and supported intents.

---

## Greetings & Identity

| Question | Expected Response |
|----------|-------------------|
| hi | Introduces as Athena, lists capabilities |
| hello | Same as above |
| hey there | Same as above |
| hi athena | Identity response |
| who are you | I am Athena, created by Krupal Vora |
| what is your name | Athena |
| who created you | Krupal Vora |

## Capabilities

| Question | Expected Response |
|----------|-------------------|
| what can you help me with | Lists all supported features |
| what else can you do in this ERP | Same |
| how can you help me | Same |

## My Username

| Question | Expected Response |
|----------|-------------------|
| what is my username | Returns the logged-in user email |
| who am i | Same |
| what is my email | Same |

## User Status

| Question | Expected Response |
|----------|-------------------|
| is user rahul.w enabled | Shows enabled/disabled status |
| is user krupal.v@solarsquare.in active | Same |
| user john.d status | Same |

---

## Roles & Permissions

### My Roles
| Question | Expected Response |
|----------|-------------------|
| what all roles do I have | Lists all roles for logged-in user |
| my roles | Same |
| what roles does krupal.v@solarsquare.in have | Lists roles for specific user |

### Users with Role
| Question | Expected Response |
|----------|-------------------|
| list users with role System Manager | Lists all users assigned that role |
| who has the role Purchase Manager | Same |
| users having access to role Stock Manager | Same |

### Role Permissions
| Question | Expected Response |
|----------|-------------------|
| what permissions does role Purchase Manager have | Lists doctypes and permission flags |
| permissions for role Stock User | Same |
| what can role Sales Manager do on Sales Order | Filtered by doctype |

### Doctype Access
| Question | Expected Response |
|----------|-------------------|
| who can access Sales Order | Lists roles with access to that doctype |
| what roles have access to Delivery Note | Same |
| roles on Purchase Receipt | Same |

### Access Check
| Question | Expected Response |
|----------|-------------------|
| can I access Sales Order | Yes/no for logged-in user |
| can user john@example.com submit Purchase Order | Same for specific user |
| do I have permission to create Stock Entry | Same |

---

## Stock & Inventory

| Question | Expected Response |
|----------|-------------------|
| stock of SLR-100W | Stock balance across warehouses (non-zero only) |
| CCOP-0001-AADITYA POLYMAKE stock | Handles multi-word item codes |
| MDCR-0025-PREMIER stock | Same |
| stock balance for item code SLR-100W | Same |
| stock of Solar Panel 100W | Fuzzy search by item name |
| what is the inventory of SLR-100W in Pune WH | Filtered by warehouse |
| top 5 stock for MDCR-0025-PREMIER | Shows only top N warehouses |

---

## Document Lookup

| Question | Expected Response |
|----------|-------------------|
| show me PO-2024-00123 | Fetches document fields |
| details of GRN-2026-03990 | Same |
| what is SO-2024-05678 | Same |
| open DN-2024-12345 | Same |
| fetch data of SE-2024-00001 | Same |

---

## Pending Approvals

| Question | Expected Response |
|----------|-------------------|
| what's pending my approval | Lists documents from Workflow Action |
| pending approvals | Same |
| documents waiting for my action | Same |
| my approval queue | Same |

---

## Open Tasks

| Question | Expected Response |
|----------|-------------------|
| my open tasks | Lists from tabToDo |
| what tasks are assigned to me | Same |
| open todos | Same |

---

## Database Queries (NL-to-SQL)

Requires roles: System Manager, Stock Manager, Accounts Manager, Purchase Manager, or Sales Manager.

### Count Queries
| Question | Expected Response |
|----------|-------------------|
| how many submitted Delivery Notes | COUNT with docstatus = 1 |
| count of Purchase Orders | COUNT(*) |
| total number of Sales Invoices | COUNT |
| how many GRNs submitted with inter_company_reference = 1 | COUNT with filter |

### Amount / Sum Queries
| Question | Expected Response |
|----------|-------------------|
| total amount of Purchase Receipt | SUM(grand_total) or SUM(rounded_total) |
| total value of submitted Sales Orders | SUM(grand_total) WHERE docstatus=1 |
| sum of all Purchase Invoice amounts | SUM query |

### Filtered Queries
| Question | Expected Response |
|----------|-------------------|
| Purchase Receipts with is_internal_supplier = 0 | COUNT/list with filter |
| Delivery Notes created till 2026-03-31 | Date-filtered query |
| list all submitted Stock Entries | SELECT with docstatus = 1 |

---

## Workflows (live `tabWorkflow*`)

Routes via `regex/workflow`. Reads `tabWorkflow`, `tabWorkflow Document State`, `tabWorkflow Transition` and scopes the answer to the user's roles (from the request payload, not a DB fetch).

| Question | Expected Response |
|----------|-------------------|
| workflow of Purchase Order | Lists all states (with Draft/Submitted/Cancelled label), splits transitions into "you can perform" vs "other" based on user_roles, shows `condition` Python expressions, ends with `/app/workflow/<name>` deep-link |
| I want to know workflow of PO | Same as above |
| states for GRN | Workflow for Purchase Receipt |
| who can approve Purchase Order | Distinct roles from approve-action transitions only (Finance Manager, PO Approval, Purchase User) |
| list all workflows | All 12 active workflows + per-workflow deep-link |
| workflow on Sales Order | Same as PO but for SO |

## How-to / Deep links (intercepts before NL→SQL)

Routes via `regex/howto`. Returns deep-link + 3 numbered steps + read-only disclaimer.

| Question | Expected Response |
|----------|-------------------|
| how to do data import for item. Give me link to do that | `/app/data-import/new?reference_doctype=Item` + 3 steps |
| I want to import data | Same as above |
| go to data import | Same as above |
| create one item with name ABC-001 | `/app/item/new?item_code=ABC-001` (extracted item code prefilled) + 3 steps |
| give me link to create a new supplier | `/app/supplier/new` + 3 steps |
| how to do stock reconciliation | `/app/stock-reconciliation/new` + 3 steps |
| how do I transfer stock between warehouses | `/app/stock-entry/new` + 3 steps |
| create a new GRN | `/app/purchase-receipt/new` + 3 steps |
| where do I create a workflow | `/app/workflow/new` + 4 steps |

## Lists (hand-written queries — bypass NL→SQL)

| Question | Expected Response |
|----------|-------------------|
| list all users | All active users with full names |
| list all warehouses | All non-disabled warehouses with type + company |
| list of all roles | All non-disabled roles |

## Compound questions (two-clause split)

| Question | Expected Response |
|----------|-------------------|
| what is workflow on GRN, and can I make B2B grn? | Q1: GRN workflow render. Q2: refusal + `/app/purchase-receipt/new` link. Joined with `---` separator |

## Read-only refusal pattern

For any **write-action** verb (create/make/add/raise/submit/update/delete) on a known doctype, Athena hands back the deep-link rather than a verbose disclaimer. Never silently routes write actions to NL→SQL.

## ERP Process / Documentation (RAG)

These go through the RAG pipeline (documentation search).

| Question | Expected Response |
|----------|-------------------|
| what is workflow in Purchase Order | Explains PO workflow from docs |
| how does Material Request work | Process explanation |
| how to create a Stock Entry | Step-by-step process |
| what is the difference between Sales Order and Sales Invoice | Conceptual explanation |
| how to do stock reconciliation | Process docs |
| what is GRN process in ERPNext | Purchase Receipt workflow |
| explain subcontracting in ERPNext | Feature explanation |
| what is BOM and how to create one | Docs on Bill of Materials |

---

## Context-Aware Questions

When the user is viewing a specific document in ERP, Athena receives `current_doctype` and `current_doc` context.

| Question | Context | Expected Response |
|----------|---------|-------------------|
| what items are in this GRN | current_doc: GRN-2026-03990 | Should query child table items |
| who approved this | current_doc: PO-2024-00123 | Should look up approval fields |
| what is the status | current_doc: DN-2024-12345 | Returns docstatus/workflow_state |

---

## Follow-up / Conversational

Athena rewrites follow-up questions using conversation history.

| Question | Prior Context | Expected |
|----------|--------------|----------|
| what about Stock Entry | Was discussing roles on PO | "What roles have access to Stock Entry?" |
| and for that role | Was asking about Purchase Manager perms | Continues with same role |
| show me the stock | Was asking about item MDCR-0025 | "Stock of MDCR-0025-PREMIER" |

---

## Edge Cases / Known Patterns

| Question | Notes |
|----------|-------|
| Sotck of MDCR-0025-PREMIERE | Typo in item code — fuzzy search suggests correct match |
| find similar item as MDCR-0025-PREMIERE | Should trigger item search (currently goes to RAG) |
| list of all workflows exists | Goes to RAG — no dedicated workflow list handler yet |
| wrong in db I can see workflow created | Conversational feedback — goes to RAG |
