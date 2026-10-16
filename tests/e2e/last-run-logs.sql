-- SPDX-License-Identifier: AGPL-3.0-or-later
-- The log of each connection's most recent run (printed when the end-to-end suite fails).
select r.connection, r.status, r.summary, r.log
from `tabImporter Run` r
where r.creation = (select max(x.creation) from `tabImporter Run` x where x.connection = r.connection)\G
