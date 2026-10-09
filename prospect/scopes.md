<!--
REVIEW BEFORE SENDING: scope names are a starting list from the hand-off and are UNVERIFIED
against the Service Key scope picker. Confirm each row in a portal we control, correct the names,
fill in the "Confirmed" column, then remove this comment.
-->

# Scopes for the Process Pro Audit Key

Add **read scopes only**. Nothing on this list lets the key change your portal.

| Area | Scope | What the audit uses it for | Confirmed in picker |
| --- | --- | --- | --- |
| Contacts, companies, deals | `crm.objects.contacts.read`, `crm.objects.companies.read`, `crm.objects.deals.read` | Totals and field-completeness percentages; open deal ages | not yet |
| Owners | `crm.objects.owners.read` | Mapping deals and activity to owners | not yet |
| Custom objects | `crm.objects.custom.read` | Record counts and field completeness on custom objects | not yet |
| Properties and schemas | `crm.schemas.contacts.read`, `crm.schemas.companies.read`, `crm.schemas.deals.read`, `crm.schemas.custom.read` | Property counts, naming, lifecycle values, custom object structure | not yet |
| Lists | `crm.lists.read` | List inventory (type, size, last updated) | not yet |
| Forms | `forms` | Form list and submission dates | not yet |
| Marketing email | `content` | Email send volume and open/click/bounce rates | not yet |
| Workflows | `automation` | Workflow inventory (name, status, last updated) | not yet |
| Sequences | `automation.sequences.read` (name to confirm) | Sequence list | not yet |
| Users | `settings.users.read` | User count, roles, super admin count | not yet |
| Subscriptions | `communication_preferences.read` | Subscription types | not yet |
| Activity | Read scopes for calls, meetings, emails and tasks (names to find in the picker) | Activity counts per owner, last 30 days | not yet |

## If a scope is missing

The audit does not fail. That area is marked "forbidden" in the results and we will ask you for a screenshot instead, or ask you to add the scope and rerun.

## Not collected

Contact names, email addresses and phone numbers are not read into the audit output. Record-level data is processed in memory only and discarded when the run ends.
