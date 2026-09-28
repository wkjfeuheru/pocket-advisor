import assert from 'node:assert/strict'
import { existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import test from 'node:test'

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const src = path.join(frontendRoot, 'src')

test('frontend pages and components are organized by feature', () => {
  for (const feature of ['auth', 'product', 'portfolio', 'admin']) {
    assert.ok(existsSync(path.join(src, 'features', feature)), `${feature} feature directory is missing`)
  }

  assert.equal(existsSync(path.join(src, 'views')), false, 'flat views directory should be removed')
  assert.equal(existsSync(path.join(src, 'components')), false, 'flat components directory should be removed')

  const featureFiles = [
    'features/auth/components/LoginView.vue',
    'features/product/views/ProductMarketView.vue',
    'features/product/api.ts',
    'features/product/types.ts',
    'features/portfolio/views/AccountView.vue',
    'features/portfolio/views/PositionsView.vue',
    'features/portfolio/api.ts',
    'features/portfolio/types.ts',
    'features/admin/components/AdminUserPanel.vue',
    'features/admin/components/AdminProductPanel.vue',
    'features/admin/api.ts',
    'features/admin/types.ts',
  ]
  for (const relativePath of featureFiles) {
    assert.ok(existsSync(path.join(src, relativePath)), `${relativePath} is missing`)
  }

  assert.equal(
    existsSync(path.join(src, 'features/portfolio/views/ProductMarketView.vue')),
    false,
    'product shelf view must live under features/product',
  )
})

test('types live in feature directories without a global type barrel', () => {
  assert.equal(existsSync(path.join(src, 'types', 'index.ts')), false, 'global types barrel should be removed')
  for (const feature of ['auth', 'product', 'portfolio', 'admin']) {
    assert.ok(existsSync(path.join(src, 'features', feature, 'types.ts')), `${feature} types are missing`)
  }
})
