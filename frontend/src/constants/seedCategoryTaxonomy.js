import seedCategoryTaxonomy from './seedCategoryTaxonomy.generated.json';

export const SEED_CATEGORY_TAXONOMY = seedCategoryTaxonomy;

export const SEED_CATEGORIES_BY_KEY = Object.fromEntries(
  seedCategoryTaxonomy.categories.map((category) => [category.seed_key, category]),
);

export const SEED_CATEGORY_PARENTS = seedCategoryTaxonomy.categories.filter(
  (category) => category.parent_id == null,
);

export function getSeedCategory(seedKey) {
  const category = SEED_CATEGORIES_BY_KEY[seedKey];
  if (!category) throw new Error(`Unknown seed category: ${seedKey}`);
  return category;
}
