import json

import pytest

from rattle.exceptions import InvalidOpenAPISpecError
from rattle.openapi import parse_openapi_spec


def test_parse_json_spec_with_params_and_body() -> None:
    raw = json.dumps(
        {
            "openapi": "3.0.3",
            "info": {"title": "Users API"},
            "paths": {
                "/users/{user_id}": {
                    "get": {
                        "operationId": "get_user",
                        "parameters": [
                            {
                                "name": "user_id",
                                "in": "path",
                                "schema": {"type": "integer", "minimum": 1},
                            },
                            {
                                "name": "verbose",
                                "in": "query",
                                "required": False,
                                "schema": {"type": "boolean"},
                            },
                            {
                                "name": "X-Trace-Id",
                                "in": "header",
                                "schema": {"type": "string", "minLength": 4},
                            },
                        ],
                    }
                },
                "/users": {
                    "post": {
                        "requestBody": {
                            "required": True,
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "required": ["name", "role"],
                                        "properties": {
                                            "name": {
                                                "type": "string",
                                                "minLength": 2,
                                                "maxLength": 50,
                                            },
                                            "role": {
                                                "type": "string",
                                                "enum": ["admin", "user"],
                                            },
                                            "age": {
                                                "type": "integer",
                                                "minimum": 0,
                                                "maximum": 120,
                                            },
                                        },
                                    }
                                }
                            },
                        }
                    }
                },
            },
        }
    )

    spec = parse_openapi_spec(raw)

    assert spec.version == "3.0.3"
    assert spec.title == "Users API"
    assert len(spec.endpoints) == 2

    get_user = spec.endpoints[0]
    assert get_user.method == "GET"
    assert get_user.path == "/users/{user_id}"
    assert get_user.operation_id == "get_user"
    assert len(get_user.parameters) == 3
    assert get_user.parameters[0].name == "user_id"
    assert get_user.parameters[0].location == "path"
    assert get_user.parameters[0].required is True
    assert get_user.parameters[0].schema.schema_type == "integer"
    assert get_user.parameters[0].schema.minimum == 1

    create_user = spec.endpoints[1]
    assert create_user.method == "POST"
    assert create_user.request_body is not None
    assert create_user.request_body.required is True
    assert create_user.request_body.content_type == "application/json"

    body_schema = create_user.request_body.schema
    assert body_schema.schema_type == "object"
    assert body_schema.required_fields == frozenset({"name", "role"})
    assert body_schema.properties["name"].min_length == 2
    assert body_schema.properties["name"].max_length == 50
    assert body_schema.properties["role"].enum == ("admin", "user")
    assert body_schema.properties["age"].maximum == 120


def test_parse_yaml_spec_with_refs_and_path_level_parameters() -> None:
    raw = """
openapi: "3.0.1"
info:
  title: Orders API
paths:
  /orders/{order_id}:
    summary: Order endpoint
    parameters:
      - $ref: "#/components/parameters/OrderId"
      - name: expand
        in: query
        required: false
        schema:
          type: boolean
    get:
      parameters:
        - name: expand
          in: query
          required: true
          schema:
            type: string
            enum: ["items", "customer"]
    post:
      requestBody:
        $ref: "#/components/requestBodies/OrderBody"
components:
  parameters:
    OrderId:
      name: order_id
      in: path
      schema:
        type: string
  requestBodies:
    OrderBody:
      required: true
      content:
        application/json:
          schema:
            $ref: "#/components/schemas/OrderInput"
  schemas:
    Address:
      type: object
      required: ["city"]
      properties:
        city:
          type: string
    OrderInput:
      type: object
      properties:
        billing:
          $ref: "#/components/schemas/Address"
        shipping:
          $ref: "#/components/schemas/Address"
        tags:
          type: array
          items:
            type: string
"""

    spec = parse_openapi_spec(raw.encode("utf-8"))
    assert len(spec.endpoints) == 2

    get_order = spec.endpoints[0]
    assert get_order.method == "GET"
    assert len(get_order.parameters) == 2
    expand_param = next(p for p in get_order.parameters if p.name == "expand")
    assert expand_param.required is True
    assert expand_param.schema.schema_type == "string"
    assert expand_param.schema.enum == ("items", "customer")

    post_order = spec.endpoints[1]
    assert post_order.request_body is not None
    assert post_order.request_body.required is True
    order_schema = post_order.request_body.schema
    assert order_schema.properties["billing"].properties["city"].schema_type == "string"
    assert order_schema.properties["shipping"].properties["city"].schema_type == "string"
    assert order_schema.properties["tags"].schema_type == "array"
    assert order_schema.properties["tags"].items is not None
    assert order_schema.properties["tags"].items.schema_type == "string"


def test_recursive_schema_ref_stops_without_infinite_recursion() -> None:
    raw = """
openapi: "3.0.3"
paths:
  /nodes:
    post:
      requestBody:
        content:
          application/json:
            schema:
              $ref: "#/components/schemas/Node"
components:
  schemas:
    Node:
      type: object
      required: ["value"]
      properties:
        value:
          type: integer
        next:
          $ref: "#/components/schemas/Node"
        children:
          type: array
          items:
            $ref: "#/components/schemas/Node"
"""

    spec = parse_openapi_spec(raw)
    body = spec.endpoints[0].request_body
    assert body is not None

    node = body.schema
    assert node.properties["value"].schema_type == "integer"
    assert node.properties["next"].is_recursive is True
    assert node.properties["children"].items is not None
    assert node.properties["children"].items.is_recursive is True


def test_direct_ref_alias_cycle_raises_error() -> None:
    raw = """
openapi: "3.0.3"
paths:
  /loop:
    post:
      requestBody:
        content:
          application/json:
            schema:
              $ref: "#/components/schemas/A"
components:
  schemas:
    A:
      $ref: "#/components/schemas/B"
    B:
      $ref: "#/components/schemas/A"
"""

    with pytest.raises(InvalidOpenAPISpecError, match="Циклическая ссылка"):
        parse_openapi_spec(raw)


@pytest.mark.parametrize(
    "raw_spec",
    [
        "",
        "   ",
        b"\xff\xfe",
        "{broken json",
        "- item1\n- item2",
        json.dumps({"swagger": "2.0", "paths": {"/ping": {"get": {}}}}),
        json.dumps({"openapi": "3.0.3"}),
        json.dumps({"openapi": "3.0.3", "paths": {}}),
        json.dumps(
            {
                "openapi": "3.0.3",
                "paths": {"bad_path": {"get": {}}},
            }
        ),
        json.dumps(
            {
                "openapi": "3.0.3",
                "paths": {"/x": {"get": {"parameters": [{"name": "a", "in": "body"}]}}},
            }
        ),
        json.dumps(
            {
                "openapi": "3.0.3",
                "paths": {
                    "/x": {
                        "post": {
                            "requestBody": {
                                "content": {
                                    "application/json": {
                                        "schema": {"$ref": "#/components/schemas/Missing"}
                                    }
                                }
                            }
                        }
                    }
                },
            }
        ),
        json.dumps(
            {
                "openapi": "3.0.3",
                "paths": {
                    "/x": {
                        "post": {
                            "requestBody": {
                                "content": {
                                    "application/json": {"schema": {"$ref": "other.yaml#/User"}}
                                }
                            }
                        }
                    }
                },
            }
        ),
    ],
)
def test_invalid_specs_raise_custom_error(raw_spec: str | bytes) -> None:
    with pytest.raises(InvalidOpenAPISpecError):
        parse_openapi_spec(raw_spec)


def test_nullable_type_list_anyof_allof_and_non_json_body() -> None:
    raw = json.dumps(
        {
            "openapi": "3.1.0",
            "paths": {
                "/items": {
                    "post": {
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "label": {"type": ["string", "null"], "minLength": 1},
                                            "count": {
                                                "anyOf": [
                                                    {"type": "integer", "minimum": 0},
                                                    {"type": "null"},
                                                ]
                                            },
                                            "extra": {
                                                "nullable": True,
                                                "allOf": [{"$ref": "#/components/schemas/Tag"}],
                                            },
                                        },
                                    }
                                }
                            }
                        }
                    }
                },
                "/upload": {
                    "post": {
                        "requestBody": {
                            "content": {"multipart/form-data": {"schema": {"type": "object"}}}
                        }
                    }
                },
            },
            "components": {
                "schemas": {
                    "Tag": {"type": "string", "maxLength": 16},
                }
            },
        }
    )

    spec = parse_openapi_spec(raw)
    assert len(spec.endpoints) == 2

    items_body = spec.endpoints[0].request_body
    assert items_body is not None
    assert items_body.schema.properties["label"].schema_type == "string"
    assert items_body.schema.properties["label"].nullable is True
    assert items_body.schema.properties["label"].min_length == 1
    assert items_body.schema.properties["count"].schema_type == "integer"
    assert items_body.schema.properties["count"].nullable is True
    assert items_body.schema.properties["count"].minimum == 0
    assert items_body.schema.properties["extra"].schema_type == "string"
    assert items_body.schema.properties["extra"].nullable is True
    assert items_body.schema.properties["extra"].max_length == 16

    upload_endpoint = spec.endpoints[1]
    assert upload_endpoint.request_body is None


def test_json_pointer_unescapes_tilde_and_slash() -> None:
    raw = json.dumps(
        {
            "openapi": "3.0.3",
            "paths": {
                "/custom": {
                    "post": {
                        "requestBody": {
                            "content": {
                                "application/problem+json": {
                                    "schema": {"$ref": "#/components/schemas/User~1Profile~0V1"}
                                }
                            }
                        }
                    }
                }
            },
            "components": {
                "schemas": {
                    "User/Profile~V1": {
                        "type": "object",
                        "properties": {"id": {"type": "integer"}},
                    }
                }
            },
        }
    )

    spec = parse_openapi_spec(raw)
    body = spec.endpoints[0].request_body
    assert body is not None
    assert body.content_type == "application/problem+json"
    assert body.schema.properties["id"].schema_type == "integer"
