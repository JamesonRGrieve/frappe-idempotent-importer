<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Coverage: QuickBooks Desktop → ERPNext

**Listed, not built:** a connection to QuickBooks Desktop can be saved, but its runs refuse to start. Nothing is
mirrored yet. This page records why, and what building it needs.

## The supported route

QuickBooks Desktop (Pro, Premier, Enterprise) keeps its company file on a Windows machine and has no network
API of its own. Intuit supports two ways in, both through the QuickBooks Desktop SDK and its request language,
qbXML:

- **QuickBooks Web Connector** (QBWC), Intuit's Windows application beside QuickBooks: it polls a SOAP web
  service on a schedule (`authenticate`, `sendRequestXML`, `receiveResponseXML`, `closeConnection`), passes the
  qbXML requests that service hands it to QuickBooks, and posts QuickBooks' answers back. The service is
  registered with QBWC by a `.qwc` file the owner opens on that machine.
- **The SDK's request processor** (`QBXMLRP2`, COM), called by a program running on the same Windows machine.

Either way the reads are qbXML queries (`AccountQueryRq`, `ItemQueryRq`, `CustomerQueryRq`, `VendorQueryRq`,
`EmployeeQueryRq`, `EstimateQueryRq`, `SalesOrderQueryRq`, `InvoiceQueryRq`, `CreditMemoQueryRq`,
`ReceivePaymentQueryRq`, `BillQueryRq`, `BillPaymentCheckQueryRq`, `CheckQueryRq`, `DepositQueryRq`,
`JournalEntryQueryRq`, `PurchaseOrderQueryRq`, `TimeTrackingQueryRq`, `TxnDeletedQueryRq`, ...), paged with
iterators (`iterator="Start"`/`"Continue"`, `MaxReturned`) and filtered by `FromModifiedDate` for incremental
runs.

## Why it isn't built

- **The importer's runs read; QBWC calls in.** Every built source is read by the importer on its own schedule.
  With the Web Connector the direction is reversed: QBWC on the owner's Windows machine calls a SOAP service on
  this site when it chooses, one request at a time, and a run can only advance while that machine is on and
  QuickBooks is open. Building it means a new inbound SOAP endpoint (whitelisted, authenticated by the `.qwc`'s
  user and a stored password) and a run that is driven by those calls rather than by the job queue: an
  architecture decision for the operator, not an adapter.
- **The schema can't be pinned from here.** Intuit publishes the qbXML schemas (`qbxmlops<version>.xsd`) only
  inside the QuickBooks Desktop SDK, a Windows installer behind an Intuit developer sign-in; its public
  Onscreen Reference renders them but is not a schema document. Without a schema pinned by version and SHA-256,
  fixtures can't be proven as the brief requires, and without a Windows host with QuickBooks Desktop and QBWC
  the adapter can't be run against the real thing even once.

## What building it needs

1. The operator's decision on the inbound route: a QBWC SOAP endpoint on this site (and how a QBWC-driven run
   fits the job queue and lockout safety), or a small agent on the Windows machine that answers the importer.
2. The SDK's qbXML schema for the targeted version (13.0, QuickBooks 2013 and later), committed or pinned.
3. A Windows host with QuickBooks Desktop, a sample company file and QBWC for one live run.

The mapping itself would follow the QuickBooks Online adapter's (`coverage/quickbooks_online.md`): the same
entities under their qbXML names, onto the same ERPNext homes.
