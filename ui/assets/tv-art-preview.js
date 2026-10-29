// Approved concept sheet: use its actual story panels, not substitute characters.
export const PANELS = [[2,85,620,454],[631,85,505,454],[1149,85,521,454]];
export function shotAt(seconds, reducedMotion=false) {
  const t=Math.max(0,Math.min(35,Number(seconds)||0));
  const cuts=[0,7,14,21,28,35], panels=[0,2,1,1,2];
  const index=Math.min(4,cuts.findIndex((end,i)=>i>0 && t<end)-1);
  const shot=index<0?4:index;
  const progress=(t-cuts[shot])/7;
  return {panel:panels[shot], zoom:reducedMotion?1:1+0.025*(progress*progress*(3-2*progress))};
}
export function paintBureau(ctx,image,seconds,width,height,reducedMotion=false) {
  ctx.fillStyle='#152323';ctx.fillRect(0,0,width,height);
  if(!image?.naturalWidth) {
    ctx.fillStyle='#f4ead5';ctx.font='18px sans-serif';ctx.textAlign='center';
    ctx.fillText(image?.dataset?.failed ? 'Artwork unavailable. Reload this preview to retry.' : 'Loading approved artwork...',width/2,height/2);
    ctx.textAlign='start';return;
  }
  const shot=shotAt(seconds,reducedMotion), [x,y,w,h]=PANELS[shot.panel];
  const scale=Math.min(width/w,height/h), dw=w*scale,dh=h*scale;
  ctx.save();ctx.beginPath();ctx.rect((width-dw)/2,(height-dh)/2,dw,dh);ctx.clip();
  ctx.imageSmoothingEnabled=true;ctx.imageSmoothingQuality='high';
  ctx.drawImage(image,x,y,w,h,(width-dw*shot.zoom)/2,(height-dh*shot.zoom)/2,dw*shot.zoom,dh*shot.zoom);
  ctx.restore();
}
