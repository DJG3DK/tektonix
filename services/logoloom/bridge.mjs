/**
 * LogoLoom, called directly instead of over MCP.
 *
 * Upstream (mcpware/logoloom, MIT) ships an MCP stdio server whose four tools
 * are thin wrappers over four local modules. Tektonix has no MCP client, and
 * adding one -- protocol, handshake, a long-lived subprocess per session --
 * to reach four pure functions in the same language would be machinery with
 * nothing on the other end of it. This imports the modules.
 *
 * Read JSON on argv[2], write JSON on stdout, exit non-zero on failure.
 * One process per call: these are seconds-long operations a handful of times
 * per task, not a hot loop.
 *
 * WHY imagePath IS NOT PASSED THROUGH
 * -----------------------------------
 * image-to-svg.mjs runs `execSync(\`vtracer --input ${imagePath} ...\`)` -- a
 * template string, so it goes through a shell. Our caller is a model, and the
 * path it picks can come from a repo file or a web page it read, which makes
 * that a live command-injection path rather than a theoretical one. So the
 * path handed to that function is never the model's: the file is copied to a
 * temp name this script generates, and the generated name is what upstream
 * interpolates. The same copy also stops a symlink pointing somewhere else.
 *
 * And vtracer is checked for up front. Without it that function falls back to
 * `npx -y vtracer-cli`, which is a network fetch and an unpinned package,
 * mid-task. Better to say plainly that colour tracing is not installed.
 */
import { execFileSync } from 'node:child_process';
import { copyFileSync, mkdtempSync, rmSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { extname, join } from 'node:path';

const MOD = '@mcpware/logoloom/src/tools/';

function have(binary) {
  try {
    execFileSync('command', ['-v', binary], { shell: '/bin/sh', stdio: 'ignore' });
    return true;
  } catch {
    return false;
  }
}

async function imageToSvgSafely({ imagePath, colorMode, precision }) {
  const mode = colorMode === 'binary' ? 'binary' : 'color';
  if (mode === 'color' && !have('vtracer')) {
    return {
      success: false,
      error:
        'colour tracing needs vtracer, which is not installed on this host ' +
        '(cargo install vtracer). Binary mode works -- it uses potrace.',
    };
  }
  if (mode === 'binary' && !have('vtracer') && !have('potrace')) {
    return { success: false, error: 'no vectorizer installed (vtracer or potrace)' };
  }

  const st = statSync(imagePath);        // throws for a missing file; caught by the caller
  if (!st.isFile()) throw new Error(`${imagePath} is not a file`);

  // The name upstream interpolates into a shell string is generated here, so
  // it holds no metacharacters whatever the model asked for.
  const dir = mkdtempSync(join(tmpdir(), 'logoloom-in-'));
  const ext = /^\.[a-z0-9]{1,5}$/i.test(extname(imagePath)) ? extname(imagePath).toLowerCase() : '.png';
  const safe = join(dir, `input${ext}`);
  try {
    copyFileSync(imagePath, safe);
    const { imageToSvg } = await import(MOD + 'image-to-svg.mjs');
    return JSON.parse(await imageToSvg(safe, mode, precision));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

async function run(op, args) {
  switch (op) {
    case 'text_to_path': {
      const { textToPath } = await import(MOD + 'text-to-path.mjs');
      return JSON.parse(await textToPath(args.svg, args.fontPath));
    }
    case 'optimize_svg': {
      const { optimizeSvg } = await import(MOD + 'optimize-svg.mjs');
      return JSON.parse(await optimizeSvg(args.svg, Boolean(args.aggressive)));
    }
    case 'export_brand_kit': {
      const { exportBrandKit } = await import(MOD + 'export-brand-kit.mjs');
      return JSON.parse(await exportBrandKit(args));
    }
    case 'image_to_svg':
      return await imageToSvgSafely(args);
    case 'render_png': {
      // Not upstream's. The model writes SVG it cannot see, which is the same
      // gap preview_app closed for a running page: sharp is already here to
      // rasterise, and the caller turns this into something it can look at.
      const sharp = (await import('sharp')).default;
      const png = await sharp(Buffer.from(args.svg, 'utf8'), { density: args.density || 144 })
        .resize({
          width: args.width || 512,
          height: args.height || 512,
          fit: 'contain',
          background: args.background || { r: 255, g: 255, b: 255, alpha: 0 },
        })
        .png()
        .toBuffer();
      return { success: true, pngBase64: png.toString('base64'), bytes: png.length };
    }
    default:
      return { success: false, error: `unknown operation ${op}` };
  }
}

const payload = JSON.parse(process.argv[2] || '{}');
try {
  const result = await run(payload.op, payload.args || {});
  process.stdout.write(JSON.stringify(result));
} catch (e) {
  process.stdout.write(JSON.stringify({ success: false, error: String(e && e.message || e) }));
  process.exit(1);
}
