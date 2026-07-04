import{n as e,t}from"./react.CD6hyuMb.js";var n=e((e=>{var t=Symbol.for(`react.transitional.element`),n=Symbol.for(`react.fragment`);function r(e,n,r){var i=null;if(r!==void 0&&(i=``+r),n.key!==void 0&&(i=``+n.key),`key`in n)for(var a in r={},n)a!==`key`&&(r[a]=n[a]);else r=n;return n=r.ref,{$$typeof:t,type:e,key:i,ref:n===void 0?null:n,props:r}}e.Fragment=n,e.jsx=r,e.jsxs=r})),r=e(((e,t)=>{t.exports=n()})),i=t(),a=r(),o=`
precision mediump float;
uniform vec2 u_res;
uniform float u_time;
float wave(vec2 p, float t){
  return sin(p.x*1.4 + t)*0.5 + sin(p.y*1.7 - t*0.8)*0.5
       + sin((p.x+p.y)*1.1 + t*0.6)*0.5;
}
void main(){
  vec2 uv = gl_FragCoord.xy / u_res.xy;
  vec2 p = (uv - 0.5) * vec2(u_res.x/u_res.y, 1.0) * 3.0;
  float t = u_time * 0.12;
  float v = wave(p, t) + wave(p*1.9 + 4.0, -t*1.3)*0.5;
  v = v*0.5 + 0.5;
  vec3 cyan = vec3(0.26, 0.86, 0.90);
  vec3 violet = vec3(0.60, 0.55, 1.0);
  vec3 pink = vec3(1.0, 0.62, 0.78);
  vec3 col = mix(cyan, violet, smoothstep(0.2, 0.7, v));
  col = mix(col, pink, smoothstep(0.6, 1.0, v) * 0.5);
  float d = distance(uv, vec2(0.5));
  float glow = smoothstep(0.9, 0.1, d);
  gl_FragColor = vec4(col * v * glow * 0.16, 1.0);
}`,s=`attribute vec2 a_pos; void main(){ gl_Position = vec4(a_pos, 0.0, 1.0); }`;function c(e,t,n){let r=e.createShader(t);return e.shaderSource(r,n),e.compileShader(r),r}function l(){let e=(0,i.useRef)(null);return(0,i.useEffect)(()=>{let t=e.current;if(!t)return;let n=null;try{n=t.getContext(`webgl`)||t.getContext(`experimental-webgl`)}catch{n=null}if(!n)return;let r=n.createProgram();n.attachShader(r,c(n,n.VERTEX_SHADER,s)),n.attachShader(r,c(n,n.FRAGMENT_SHADER,o)),n.linkProgram(r),n.useProgram(r);let i=n.createBuffer();n.bindBuffer(n.ARRAY_BUFFER,i),n.bufferData(n.ARRAY_BUFFER,new Float32Array([-1,-1,3,-1,-1,3]),n.STATIC_DRAW);let a=n.getAttribLocation(r,`a_pos`);n.enableVertexAttribArray(a),n.vertexAttribPointer(a,2,n.FLOAT,!1,0,0);let l=n.getUniformLocation(r,`u_res`),u=n.getUniformLocation(r,`u_time`);function d(){let e=Math.min(window.devicePixelRatio||1,1.5);t.width=Math.floor(window.innerWidth*e),t.height=Math.floor(window.innerHeight*e),n.viewport(0,0,t.width,t.height)}d(),window.addEventListener(`resize`,d);let f,p=null,m=!1;function h(e){m||(p===null&&(p=e),n.uniform2f(l,t.width,t.height),n.uniform1f(u,(e-p)/1e3),n.drawArrays(n.TRIANGLES,0,3),f=requestAnimationFrame(h))}return f=requestAnimationFrame(h),()=>{m=!0,cancelAnimationFrame(f),window.removeEventListener(`resize`,d)}},[]),(0,a.jsx)(`canvas`,{ref:e,className:`rsh-shader`,"aria-hidden":`true`,"data-testid":`rsh-shader`})}export{r as n,l as t};