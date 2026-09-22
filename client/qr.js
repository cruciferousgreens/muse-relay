/* QR wrapper around qrcode-generator (bundled). */
import qrcode from "qrcode-generator";

export class QRCode {
  constructor({ content, width = 200, height = 200 }) {
    const qr = qrcode(0, "M");
    qr.addData(content);
    qr.make();
    this._tag = qr.createSvgTag({ scalable: true });
    this._w = width;
    this._h = height;
  }
  svg() {
    const t = document.createElement("template");
    t.innerHTML = this._tag.trim();
    const el = t.content.firstChild;
    el.setAttribute("width", String(this._w));
    el.setAttribute("height", String(this._h));
    return el;
  }
}
