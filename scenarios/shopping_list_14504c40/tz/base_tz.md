# Список покупок

## Builder memory
- Keep durable product decisions, domain vocabulary, UX preferences, and constraints here.
- Treat the initial scaffold as a starting point only; the current webui.json is the UI source of truth.
- Future Builder turns may replace fields, widgets, mock data, layout, and copy when the user asks.

## Assumptions
- The initial scaffold started with fields: Товар, Кол-во, Категория, Куплено; this list is not a fixed product contract

## Expected behavior
- The user can add records through the form and inspect them in the list
- Follow-up Builder turns patch the current draft and refresh the preview

## Preview notes
- Scenario shopping_list_14504c40 has a form, table, mock data, and declarative webui.json
- Data is stored in an internal CRUD datasource named shopping_items

## Risks
- No external network, device-control, or credential access is requested
- Validation and human review are still required before activation
