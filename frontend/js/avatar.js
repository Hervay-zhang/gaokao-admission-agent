/** 头像展示与本地压缩上传工具。 */

import { h } from "./ui.js";

const MAX_INPUT_BYTES = 4 * 1024 * 1024;
const OUTPUT_SIZE = 192;

export function avatarNode(user, nickname, className) {
  const url = user?.avatarUrl || "";
  if (url) {
    return h("img", {
      class: `${className} ${className}--image`,
      src: url,
      alt: `${nickname || "用户"}头像`,
    });
  }
  return h("span", { class: className }, user?.avatar || (nickname || "志").slice(0, 1));
}

function loadImage(file) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const image = new Image();
    image.onload = () => {
      URL.revokeObjectURL(url);
      resolve(image);
    };
    image.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error("图片读取失败"));
    };
    image.src = url;
  });
}

export async function fileToAvatarDataUrl(file) {
  if (!file || !file.type.startsWith("image/")) {
    throw new Error("请选择图片文件");
  }
  if (file.size > MAX_INPUT_BYTES) {
    throw new Error("原始图片不能超过 4MB");
  }
  const image = await loadImage(file);
  const canvas = document.createElement("canvas");
  canvas.width = OUTPUT_SIZE;
  canvas.height = OUTPUT_SIZE;
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("浏览器不支持头像处理");

  const sourceRatio = image.width / image.height;
  const targetRatio = 1;
  let sx = 0;
  let sy = 0;
  let sw = image.width;
  let sh = image.height;
  if (sourceRatio > targetRatio) {
    sw = image.height * targetRatio;
    sx = (image.width - sw) / 2;
  } else {
    sh = image.width / targetRatio;
    sy = (image.height - sh) / 2;
  }
  ctx.drawImage(image, sx, sy, sw, sh, 0, 0, OUTPUT_SIZE, OUTPUT_SIZE);
  return canvas.toDataURL("image/jpeg", 0.84);
}
