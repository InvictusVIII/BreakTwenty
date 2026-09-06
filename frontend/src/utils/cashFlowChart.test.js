import { rollupByParent } from './cashFlowChart';

describe('cash-flow category family rollup', () => {
  it('keeps both parent theme colors on the rolled-up family', () => {
    const [family] = rollupByParent([{
      category_id: 91,
      name: 'Other',
      color_dark: '#111111',
      color_light: '#eeeeee',
      parent_id: 89,
      parent_name: 'Other',
      parent_color_dark: '#ffffff',
      parent_color_light: '#000000',
      amount: 25,
      transaction_count: 1,
    }]);

    expect(family).toMatchObject({
      category_id: 89,
      color_dark: '#ffffff',
      color_light: '#000000',
    });
  });
});
