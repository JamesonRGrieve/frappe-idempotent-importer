# SPDX-License-Identifier: AGPL-3.0-or-later
"""Recovering a Swagger 2 document from its Spectacle rendering (tools/spectacle.py), on panels shaped as
NetSuite's REST API Browser renders them: a plain object, an array of references, an inline object with an
enum (its hidden items included), and a GET whose 200 response names a definition."""

from __future__ import annotations

from tools.spectacle import swagger_of

PAGE = """
<div id="definition-nsLink" class="definition panel" data-traverse-target="definition-nsLink">
<h2 class="panel-title"><a name="/definitions/nsLink"></a>nsLink </h2>
<div class="doc-row"><div class="doc-copy"><section class="json-schema-properties"><dl>
<dt data-property-name="href">
<span class="json-property-name">href</span>:
<span class="json-property-type">string</span>
<span class="json-property-range" title="Value limits"></span><br>
</dt>
</dl></section></div></div>
</div>
<div id="definition-account" class="definition panel" data-traverse-target="definition-account">
<div class="doc-row"><div class="doc-copy"><section class="json-schema-properties"><dl>
<dt data-property-name="acctType">
<span class="json-property-name">acctType</span>:
<span class="json-property-type">object</span>
</dt>
<dt class="json-inner-schema">
<section class="json-schema-properties"><dl>
<dt data-property-name="id">
<span class="json-property-name">id</span>:
<span class="json-property-type">string</span>
<span class="json-property-enum">
<span class="json-property-enum-item">Bank</span> ,
<span class="json-property-enum-item-hidden json-property-enum-item">AcctRec</span>
<span class="json-property-enum-item-hidden">,</span>
</span>
</dt>
</dl></section>
</dt>
<dt data-property-name="balance">
<span class="json-property-name">balance</span>:
<span class="json-property-type">number</span>
<span class="json-property-format">(double)</span>
</dt>
<dt data-property-name="links">
<span class="json-property-name">links</span>:
<span class="json-property-type"><a class="json-schema-ref" href="#/definitions/nsLink">nsLink</a></span>
</dt>
<dt class="json-inner-schema">
<section class="json-schema-array-items">
<span class="json-property-type"><a class="json-schema-ref" href="#/definitions/nsLink">nsLink</a></span>
<div class="json-inner-schema"> </div>
</section>
</dt>
<dt data-property-name="parent">
<span class="json-property-name">parent</span>:
<span class="json-property-type"><a class="json-schema-ref" href="#/definitions/account">account</a></span>
</dt>
</dl></section></div></div>
</div>
<div id="definition-empty" class="definition panel"></div>
<div id="operation--account--id--get" class="operation panel">
<section class="swagger-operation-path">
<span class="operation-method">GET</span>
<span class="operation-path">/account/{id}</span>
</section>
<section class="swagger-responses">
<div class="prop-row prop-group"><div class="prop-name"><div class="prop-title">200 OK</div>
<div class="prop-ref"><a class="json-schema-ref" href="#/definitions/account">account</a></div></div></div>
<div class="prop-row prop-group"><div class="prop-name"><div class="prop-title">default </div>
<div class="prop-ref"><a class="json-schema-ref" href="#/definitions/nsError">nsError</a></div></div></div>
</section>
</div>
<div id="operation--account-delete" class="operation panel">
<section class="swagger-operation-path">
<span class="operation-method">DELETE</span>
<span class="operation-path">/account</span>
</section>
<section class="swagger-responses">
<div class="prop-row prop-group"><div class="prop-name"><div class="prop-title">204 No Content</div></div></div>
</section>
</div>
<div id="operation--untitled" class="operation panel"><p>No method or path.</p></div>
"""


def test_definitions_keep_types_formats_enums_references_and_inner_schemas() -> None:
	definitions = swagger_of(PAGE)["definitions"]
	assert definitions["nsLink"] == {"type": "object", "properties": {"href": {"type": "string"}}}
	account = definitions["account"]["properties"]
	assert account["acctType"] == {
		"type": "object",
		"properties": {"id": {"type": "string", "enum": ["Bank", "AcctRec"]}},
	}
	assert account["balance"] == {"type": "number", "format": "double"}
	assert account["links"] == {"type": "array", "items": {"$ref": "#/definitions/nsLink"}}
	assert account["parent"] == {"$ref": "#/definitions/account"}
	assert definitions["empty"] == {"type": "object"}


def test_operations_keep_only_their_ok_response() -> None:
	document = swagger_of(PAGE)
	assert document["swagger"] == "2.0"
	assert document["paths"] == {
		"/account/{id}": {"get": {"responses": {"200": {"schema": {"$ref": "#/definitions/account"}}}}},
		"/account": {"delete": {"responses": {}}},
	}
