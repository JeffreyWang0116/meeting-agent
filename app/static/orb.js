/*
  即時聆聽的聲控光球 — 原生 WebGL，無外部套件。

  視覺全部是一支 fragment shader：畫面用一個蓋滿螢幕的三角形當畫布，
  每個像素依 simplex 雜訊場算出顏色，所以「球」其實沒有幾何模型。

  此檔不碰麥克風。音量由外部用 setLevel(0~1) 餵進來——即時聆聽已經開了
  一份麥克風串流給 MediaRecorder，共用那份即可（見 app.js 的 AnalyserNode），
  不需要也不應該再開第二份。
*/
(function () {
  "use strict";

  // shader 內建的紫色基底，要轉到本站主題色 #2f9e84（青綠）所需的 YIQ 色相角度。
  // 想換整顆球的色調就改這個值（單位：度）。
  const ORB_HUE = 113;

  const VERT = `
    precision highp float;
    attribute vec2 position;
    attribute vec2 uv;
    varying vec2 vUv;
    void main() {
      vUv = uv;
      gl_Position = vec4(position, 0.0, 1.0);
    }
  `;

  const FRAG = `
    precision highp float;

    uniform float iTime;
    uniform vec3 iResolution;
    uniform float hue;
    uniform float hover;
    uniform float rot;
    uniform float hoverIntensity;
    varying vec2 vUv;

    vec3 rgb2yiq(vec3 c) {
      float y = dot(c, vec3(0.299, 0.587, 0.114));
      float i = dot(c, vec3(0.596, -0.274, -0.322));
      float q = dot(c, vec3(0.211, -0.523, 0.312));
      return vec3(y, i, q);
    }

    vec3 yiq2rgb(vec3 c) {
      float r = c.x + 0.956 * c.y + 0.621 * c.z;
      float g = c.x - 0.272 * c.y - 0.647 * c.z;
      float b = c.x - 1.106 * c.y + 1.703 * c.z;
      return vec3(r, g, b);
    }

    vec3 adjustHue(vec3 color, float hueDeg) {
      float hueRad = hueDeg * 3.14159265 / 180.0;
      vec3 yiq = rgb2yiq(color);
      float cosA = cos(hueRad);
      float sinA = sin(hueRad);
      float i = yiq.y * cosA - yiq.z * sinA;
      float q = yiq.y * sinA + yiq.z * cosA;
      yiq.y = i;
      yiq.z = q;
      return yiq2rgb(yiq);
    }

    vec3 hash33(vec3 p3) {
      p3 = fract(p3 * vec3(0.1031, 0.11369, 0.13787));
      p3 += dot(p3, p3.yxz + 19.19);
      return -1.0 + 2.0 * fract(vec3(
        p3.x + p3.y,
        p3.x + p3.z,
        p3.y + p3.z
      ) * p3.zyx);
    }

    float snoise3(vec3 p) {
      const float K1 = 0.333333333;
      const float K2 = 0.166666667;
      vec3 i = floor(p + (p.x + p.y + p.z) * K1);
      vec3 d0 = p - (i - (i.x + i.y + i.z) * K2);
      vec3 e = step(vec3(0.0), d0 - d0.yzx);
      vec3 i1 = e * (1.0 - e.zxy);
      vec3 i2 = 1.0 - e.zxy * (1.0 - e);
      vec3 d1 = d0 - (i1 - K2);
      vec3 d2 = d0 - (i2 - K1);
      vec3 d3 = d0 - 0.5;
      vec4 h = max(0.6 - vec4(
        dot(d0, d0),
        dot(d1, d1),
        dot(d2, d2),
        dot(d3, d3)
      ), 0.0);
      vec4 n = h * h * h * h * vec4(
        dot(d0, hash33(i)),
        dot(d1, hash33(i + i1)),
        dot(d2, hash33(i + i2)),
        dot(d3, hash33(i + 1.0))
      );
      return dot(vec4(31.316), n);
    }

    vec4 extractAlpha(vec3 colorIn) {
      float a = max(max(colorIn.r, colorIn.g), colorIn.b);
      return vec4(colorIn.rgb / (a + 1e-5), a);
    }

    const vec3 baseColor1 = vec3(0.611765, 0.262745, 0.996078);
    const vec3 baseColor2 = vec3(0.298039, 0.760784, 0.913725);
    const vec3 baseColor3 = vec3(0.062745, 0.078431, 0.600000);
    const float innerRadius = 0.6;
    const float noiseScale = 0.65;

    float light1(float intensity, float attenuation, float dist) {
      return intensity / (1.0 + dist * attenuation);
    }

    float light2(float intensity, float attenuation, float dist) {
      return intensity / (1.0 + dist * dist * attenuation);
    }

    vec4 draw(vec2 uv) {
      vec3 color1 = adjustHue(baseColor1, hue);
      vec3 color2 = adjustHue(baseColor2, hue);
      vec3 color3 = adjustHue(baseColor3, hue);

      float ang = atan(uv.y, uv.x);
      float len = length(uv);
      float invLen = len > 0.0 ? 1.0 / len : 0.0;

      float n0 = snoise3(vec3(uv * noiseScale, iTime * 0.5)) * 0.5 + 0.5;
      float r0 = mix(mix(innerRadius, 1.0, 0.4), mix(innerRadius, 1.0, 0.6), n0);
      float d0 = distance(uv, (r0 * invLen) * uv);
      float v0 = light1(1.0, 10.0, d0);
      v0 *= smoothstep(r0 * 1.05, r0, len);
      float cl = cos(ang + iTime * 2.0) * 0.5 + 0.5;

      float a = iTime * -1.0;
      vec2 pos = vec2(cos(a), sin(a)) * r0;
      float d = distance(uv, pos);
      float v1 = light2(1.5, 5.0, d);
      v1 *= light1(1.0, 50.0, d0);

      float v2 = smoothstep(1.0, mix(innerRadius, 1.0, n0 * 0.5), len);
      float v3 = smoothstep(innerRadius, mix(innerRadius, 1.0, 0.5), len);

      vec3 col = mix(color1, color2, cl);
      col = mix(color3, col, v0);
      col = (col + v1) * v2 * v3;
      col = clamp(col, 0.0, 1.0);

      return extractAlpha(col);
    }

    vec4 mainImage(vec2 fragCoord) {
      vec2 center = iResolution.xy * 0.5;
      float size = min(iResolution.x, iResolution.y);
      vec2 uv = (fragCoord - center) / size * 2.0;

      float angle = rot;
      float s = sin(angle);
      float c = cos(angle);
      uv = vec2(c * uv.x - s * uv.y, s * uv.x + c * uv.y);

      uv.x += hover * hoverIntensity * 0.1 * sin(uv.y * 10.0 + iTime);
      uv.y += hover * hoverIntensity * 0.1 * sin(uv.x * 10.0 + iTime);

      return draw(uv);
    }

    void main() {
      vec2 fragCoord = vUv * iResolution.xy;
      vec4 col = mainImage(fragCoord);
      gl_FragColor = vec4(col.rgb * col.a, col.a);
    }
  `;

  function compile(gl, type, src) {
    const sh = gl.createShader(type);
    gl.shaderSource(sh, src);
    gl.compileShader(sh);
    if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
      const log = gl.getShaderInfoLog(sh);
      gl.deleteShader(sh);
      throw new Error(log || "shader 編譯失敗");
    }
    return sh;
  }

  /**
   * 在 container 內建立光球。回傳 null 代表這台裝置/瀏覽器不支援 WebGL，
   * 呼叫端應改用 CSS 動畫備援。
   *
   * 回傳物件：
   *   setLevel(v)  餵入 0~1 的音量，驅動轉速與扭曲
   *   destroy()    停止動畫、釋放 GL 資源、移除 canvas
   */
  window.createVoiceOrb = function (container) {
    const canvas = document.createElement("canvas");
    const gl =
      canvas.getContext("webgl", { alpha: true, premultipliedAlpha: false, antialias: true }) ||
      canvas.getContext("experimental-webgl", { alpha: true, premultipliedAlpha: false });
    if (!gl) return null;

    let program;
    try {
      const vs = compile(gl, gl.VERTEX_SHADER, VERT);
      const fs = compile(gl, gl.FRAGMENT_SHADER, FRAG);
      program = gl.createProgram();
      gl.attachShader(program, vs);
      gl.attachShader(program, fs);
      gl.linkProgram(program);
      gl.deleteShader(vs);
      gl.deleteShader(fs);
      if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
        throw new Error(gl.getProgramInfoLog(program) || "shader 連結失敗");
      }
    } catch (err) {
      console.warn("光球初始化失敗：", err);
      return null;
    }

    gl.useProgram(program);
    gl.clearColor(0, 0, 0, 0);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);

    // 蓋滿畫面的單一大三角形：比兩個三角形組成的矩形少一次頂點處理，
    // 且沒有對角線接縫
    const posBuf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, posBuf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    const posLoc = gl.getAttribLocation(program, "position");
    gl.enableVertexAttribArray(posLoc);
    gl.vertexAttribPointer(posLoc, 2, gl.FLOAT, false, 0, 0);

    const uvBuf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, uvBuf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([0, 0, 2, 0, 0, 2]), gl.STATIC_DRAW);
    const uvLoc = gl.getAttribLocation(program, "uv");
    gl.enableVertexAttribArray(uvLoc);
    gl.vertexAttribPointer(uvLoc, 2, gl.FLOAT, false, 0, 0);

    const uTime = gl.getUniformLocation(program, "iTime");
    const uRes = gl.getUniformLocation(program, "iResolution");
    const uHue = gl.getUniformLocation(program, "hue");
    const uHover = gl.getUniformLocation(program, "hover");
    const uRot = gl.getUniformLocation(program, "rot");
    const uHoverIntensity = gl.getUniformLocation(program, "hoverIntensity");
    gl.uniform1f(uHue, ORB_HUE);
    // 先給非零預設值：shader 拿 iResolution 當除數，若第一次 resize 還沒跑到就繪製會除以零
    gl.uniform3f(uRes, 1, 1, 1);

    canvas.style.width = "100%";
    canvas.style.height = "100%";
    canvas.style.display = "block";
    container.appendChild(canvas);

    function resize() {
      // 手機 GPU 撐不住全螢幕高 DPR 的逐像素雜訊，上限壓在 2
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const w = Math.round(container.clientWidth * dpr);
      const h = Math.round(container.clientHeight * dpr);
      if (!w || !h || (canvas.width === w && canvas.height === h)) return;
      canvas.width = w;
      canvas.height = h;
      gl.viewport(0, 0, w, h);
      gl.uniform3f(uRes, w, h, w / h);
    }
    resize();

    const ro = typeof ResizeObserver === "function" ? new ResizeObserver(resize) : null;
    if (ro) ro.observe(container);
    else window.addEventListener("resize", resize);

    let raf = 0;
    let lastTime = 0;
    let rotation = 0;
    let level = 0;      // 外部餵進來的目標音量
    let smoothed = 0;   // 平滑後實際使用的值，避免逐格跳動
    let alive = true;

    function frame(t) {
      if (!alive) return;
      raf = requestAnimationFrame(frame);
      resize();
      if (!canvas.width || !canvas.height) return;

      const dt = lastTime ? Math.min((t - lastTime) * 0.001, 0.05) : 0;
      lastTime = t;

      // 往目標值靠攏：升得快（說話要立刻有反應）、降得慢（不會一停頓就熄掉）
      smoothed += (level - smoothed) * (level > smoothed ? 0.35 : 0.08);

      // 靜音時仍緩慢自轉，球才像「醒著在聽」而不是當掉
      rotation += dt * (0.3 + smoothed * 2.4);

      gl.uniform1f(uTime, t * 0.001);
      gl.uniform1f(uRot, rotation);
      gl.uniform1f(uHover, Math.min(smoothed * 2.0, 1.0));
      gl.uniform1f(uHoverIntensity, Math.min(smoothed * 0.64, 0.8));

      gl.clear(gl.COLOR_BUFFER_BIT);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
    }
    raf = requestAnimationFrame(frame);

    return {
      setLevel(v) {
        level = Math.max(0, Math.min(1, v || 0));
      },
      destroy() {
        alive = false;
        cancelAnimationFrame(raf);
        if (ro) ro.disconnect();
        else window.removeEventListener("resize", resize);
        gl.deleteBuffer(posBuf);
        gl.deleteBuffer(uvBuf);
        gl.deleteProgram(program);
        const lose = gl.getExtension("WEBGL_lose_context");
        if (lose) lose.loseContext();
        if (canvas.parentNode) canvas.parentNode.removeChild(canvas);
      },
    };
  };
})();
