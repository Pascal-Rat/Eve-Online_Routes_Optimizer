import { schemas } from "./contract_schemas.js";

/** @param {unknown} value @param {import('./contracts').Schema} schema @param {string} path */
function validate(value, schema, path) {
  if ("ref" in schema) return validate(value, schemas[schema.ref], path);
  if ("union" in schema) {
    for (const option of schema.union) {
      try { validate(value, option, path); return; }
      catch (error) { if (!(error instanceof TypeError)) throw error; }
    }
    throw new TypeError(`${path} does not match its declared type.`);
  }
  if ("literal" in schema) {
    if (!schema.literal.includes(value)) throw new TypeError(`${path} has an unsupported value.`);
  } else if ("array" in schema) {
    if (!Array.isArray(value)) throw new TypeError(`${path} must be an array.`);
    for (const item of value) validate(item, schema.array, `${path}[]`);
  } else if ("object" in schema || "values" in schema) {
    if (value === null || typeof value !== "object" || Array.isArray(value)) {
      throw new TypeError(`${path} must be an object.`);
    }
    const fields = /** @type {Record<string, unknown>} */ (value);
    if ("object" in schema) {
      for (const required of schema.required) {
        if (!Object.hasOwn(fields, required)) throw new TypeError(`${path}.${required} is required.`);
      }
      for (const [key, field] of Object.entries(schema.object)) {
        if (Object.hasOwn(fields, key)) validate(fields[key], field, `${path}.${key}`);
      }
    } else {
      for (const [key, field] of Object.entries(fields)) validate(field, schema.values, `${path}.${key}`);
    }
  } else {
    const valid = schema.type === "null" ? value === null
      : schema.type === "integer" ? Number.isSafeInteger(value)
      : schema.type === "number" ? typeof value === "number" && Number.isFinite(value)
      : typeof value === schema.type;
    if (!valid) throw new TypeError(`${path} has an invalid primitive type.`);
  }
}

/**
 * The cast is confined to this boundary, after validation against generated schemas.
 * @template {keyof import('./contracts').ContractTypes} K
 * @param {unknown} value
 * @param {K} contract
 * @returns {import('./contracts').ContractTypes[K]}
 */
export function decode(value, contract) {
  validate(value, schemas[contract], contract);
  return /** @type {import('./contracts').ContractTypes[K]} */ (value);
}
