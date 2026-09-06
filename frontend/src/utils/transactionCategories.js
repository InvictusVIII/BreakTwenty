import { API } from '../config';
import { isPlainObject, readJsonResponse } from './apiResponse';

export async function loadTransactionCategories() {
  const response = await fetch(`${API}/categories`);
  const payload = await readJsonResponse(response, {
    label: 'Categories',
    validate: (value) => isPlainObject(value) && Array.isArray(value.categories),
  });
  return payload.categories;
}
