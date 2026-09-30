import { copyFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'

const directory = join(process.cwd(), 'dist', 'assets')
const files = readdirSync(directory)
const currentScript = files.find(file => /^index-[\w-]+\.js$/.test(file))
const currentStyle = files.find(file => /^index-[\w-]+\.css$/.test(file))

if (!currentScript || !currentStyle) {
  throw new Error('The frontend build did not create its expected assets')
}

// Older cached app shells can still reference these names after deployment.
for (const [source, aliases] of [
  [currentScript, ['index-C46-qiFh.js']],
  [currentStyle, ['index-iisHdd7A.css']],
]) {
  for (const alias of aliases) {
    if (source !== alias) copyFileSync(join(directory, source), join(directory, alias))
  }
}
