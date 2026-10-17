'use strict';
// The approved Comet animation, drawn at the panel's current size.
const cometPet = (() => {
  const canvas = document.getElementById('activity-pet');
  const tile = document.getElementById('activity-overlay');
  const c = canvas.getContext('2d');
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  // The overlay uses a fixed warm light surface, independent of the OS theme.
  const appearance = {matches:false};
  const state = {mood:'unavailable'};
  let t = 0, last = 0, raf = 0, nativeVisible = true;
  function ellipse(x,y,rx,ry,color,angle=0){c.fillStyle=color;c.beginPath();c.ellipse(x,y,rx,ry,angle,0,Math.PI*2);c.fill();}
  function stroke(points,color,width){c.strokeStyle=color;c.lineWidth=width;c.lineCap='round';c.beginPath();c.moveTo(...points[0]);points.slice(1).forEach(p=>c.lineTo(...p));c.stroke();}
  function palette(){const d=appearance.matches;return {
    ink:d?'#192030':'#242139',shine:d?'#f5fcff':'#ffffff',shadow:d?'#b9c2da':'#43365d',
    body:d?'#89e9d4':'#44baa5',tail:d?'#64bfe9':'#559dce',sad:d?'#8cbbc3':'#75a1ae',
    dead:d?'#99a8a7':'#899d9a',deadTail:d?'#718583':'#718d87',tear:d?'#80d9ff':'#3988ca',
    tongue:d?'#ffa9bd':'#e7839f',
    rainbow:d?['#ff9eaf','#ffc284','#eee58c','#8ee2b6','#8bc7ff','#c3a6ff']:['#e47193','#e69d4e','#d3bd49','#58b384','#6199dc','#a783df']
  };}
  function face(p){
    const dead=state.mood==='dead',sad=state.mood==='sad';
    if(state.mood==='unavailable'){
      stroke([[-19,-7],[-9,-7]],p.ink,3);stroke([[9,-7],[19,-7]],p.ink,3);
      stroke([[-6,13],[6,13]],p.ink,3);return;
    }
    if(dead){
      for(const x of [-14,14]){stroke([[x-5,-13],[x+5,-3]],p.ink,3.5);stroke([[x+5,-13],[x-5,-3]],p.ink,3.5);}
      stroke([[-9,12],[10,12]],p.ink,3);
      ellipse(5,18,5,7,p.tongue);stroke([[5,14],[5,18]],p.ink,1.4);
      return;
    }
    const blink=Math.sin(t*1.1)>.992;
    for(const x of [-14,14]){
      ellipse(x,-6,4.5,blink?1:sad?5:7,p.ink);
      if(!blink)ellipse(x+1,-8,1.3,1.7,p.shine);
      if(sad)stroke([[x-6,-16+(x<0?3:-2)],[x+6,-16+(x<0?-2:3)]],p.ink,2.6);
    }
    c.strokeStyle=p.ink;c.lineWidth=3;c.lineCap='round';c.beginPath();
    c.moveTo(-7,sad?16:11);c.quadraticCurveTo(0,sad?7:21,7,sad?16:11);c.stroke();
    if(sad){
      for(const [x,offset] of [[-17,0],[17,.48]]){
        const f=(t*.5+offset)%1;
        c.globalAlpha=1-f*.8;
        ellipse(x,6+f*37,3.5,5.5,p.tear);
      }c.globalAlpha=1;
    }
  }
  function draw(){
    const width=canvas.clientWidth,height=canvas.clientHeight,dpr=Math.min(devicePixelRatio||1,2);
    if(!width||!height)return;
    const scale=Math.min(height/210,width/160),w=160;
    const offsetX=(width-w*scale)/2,offsetY=(height-210*scale)/2;
    if(canvas.width!==Math.round(width*dpr)||canvas.height!==height*dpr){canvas.width=Math.round(width*dpr);canvas.height=height*dpr;}
    c.setTransform(1,0,0,1,0,0);c.clearRect(0,0,canvas.width,canvas.height);
    c.setTransform(dpr*scale,0,0,dpr*scale,offsetX*dpr,offsetY*dpr);
    const p=palette(),dead=state.mood==='dead',sad=['sad','unavailable'].includes(state.mood),wild=state.mood==='rainbow';
    // Gentle paths keep the companion present without competing with the counters.
    // Trail samples follow that path and remain inside the character column.
    const speed=wild?2:1.65;
    function flight(time){const phase=time*speed;return {
      x:w/2+Math.sin(phase)*(wild?22:18),
      y:100+(wild?Math.cos(phase)*30:Math.sin(phase*2)*18)
    };}
    const head=flight(t);
    const x=dead?66:sad?w/2+Math.sin(t*.65)*3:head.x;
    const y=dead?105:sad?98+Math.sin(t*1.3)*4:head.y;
    c.globalAlpha=.13;ellipse(w/2,dead?140:sad?160:174,dead?66:sad?43:28,dead?7:5,p.shadow);c.globalAlpha=1;
    if(dead||sad){
      // A weighted, hanging tail; no flight trail in these states.
      for(let i=28;i>=1;i--){const u=i/28;
        const tx=dead?x+u*64:x+u*43+Math.sin(u*3+t*.65)*3;
        const ty=dead?121+Math.sin(u*4)*3:y+u*48;
        ellipse(tx,ty,Math.max(2,27*(1-u)),Math.max(2,(dead?13:22)*(1-u)),dead?p.deadTail:p.tail);
      }
    }else{
      for(let i=24;i>=1;i--){const past=flight(t-i*.035);
        c.globalAlpha=(1-i/28)*.3;
        ellipse(past.x-i*.35,past.y+i*.4,Math.max(2,(39-i)*.78),Math.max(2,(33-i*.8)*.78),wild?p.rainbow[Math.floor((i-1)/4)%6]:p.tail);
      }c.globalAlpha=1;
    }
    if(wild)for(let i=0;i<4;i++){
      const a=i*Math.PI/2+t*.35,r=35+Math.sin(t+i)*2;
      c.globalAlpha=.25;ellipse(x+Math.cos(a)*r,y+Math.sin(a)*r*.65,2,2,p.rainbow[i%6]);
    }c.globalAlpha=1;
    c.save();c.translate(x,y);c.rotate(dead?.22:sad?Math.sin(t*.65)*.035:Math.cos(t*speed)*.08);
    if(!dead&&!sad)c.scale(.78,.78);
    if(dead)c.scale(1.14,.8);
    else if(sad){const sigh=Math.sin(t*1.3);c.scale(1+sigh*.025,1-sigh*.03);}
    const body=dead?p.dead:sad?p.sad:p.body;
    if(dead||sad){
      ellipse(-32,-17,12,24,body,-1.05);ellipse(32,-17,12,24,body,1.05);
    }else{
      ellipse(-20,-22,14,25,body,-.65);ellipse(22,-22,12,23,body,.65);
    }
    ellipse(0,0,43,38,body);
    c.globalAlpha=dead?.12:.3;ellipse(-13,-18,14,7,p.shine,-.4);c.globalAlpha=1;
    face(p);c.restore();
  }
  function moving() {
    return !reduced.matches && !document.hidden && !tile.hidden && nativeVisible
      && ['sad','happy','rainbow'].includes(state.mood);
  }
  function frame(now) {
    raf = 0;
    if (!moving()) return;
    // Limit drawing to 30fps; use elapsed time so the flight speed stays stable.
    if (now-last >= 1000/30) {
      t += Math.min((now-last)/1000,.1)*1.25; last = now; draw();
    }
    raf = requestAnimationFrame(frame);
  }
  function render() {
    if (raf) cancelAnimationFrame(raf);
    raf = 0; draw(); last = performance.now();
    if (moving()) raf = requestAnimationFrame(frame);
  }
  function setMood(mood) {
    if (state.mood !== mood) { state.mood = mood; t = 0; render(); }
  }
  function setVisible(visible) { nativeVisible = visible; render(); }
  reduced.addEventListener('change', render);
  document.addEventListener('visibilitychange', render);
  new ResizeObserver(render).observe(canvas);
  new MutationObserver(render).observe(tile, {attributes:true, attributeFilter:['hidden','class']});
  render();
  return {setMood, setVisible};
})();
