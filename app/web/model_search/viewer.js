// Three.js 离线渲染入口；npm run build 生成随软件分发的模块。
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';

function disposeObject(root) {
  root?.traverse(node => {
    node.geometry?.dispose();
    const materials = Array.isArray(node.material) ? node.material : [node.material];
    for (const material of materials) {
      if (!material) continue;
      for (const value of Object.values(material)) if (value?.isTexture) value.dispose();
      material.dispose();
    }
  });
}

async function loadModel(url, token, signal) {
  const response = await fetch(url, { headers: { 'X-Search-Token': token }, signal });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.error || '三维预览加载失败');
  }
  const gltf = await new GLTFLoader().parseAsync(await response.arrayBuffer(), '');
  const object = gltf.scene;
  object.traverse(node => {
    if (!node.isMesh) return;
    if (!node.geometry.attributes.normal) node.geometry.computeVertexNormals();
    const old = Array.isArray(node.material) ? node.material : [node.material];
    old.forEach(material => {
      for (const value of Object.values(material || {})) if (value?.isTexture) value.dispose();
      material?.dispose();
    });
    node.material = new THREE.MeshStandardMaterial({ color: 0x91aca0, metalness: 0.25,
      roughness: 0.42, side: THREE.DoubleSide, flatShading: true });
  });
  const box = new THREE.Box3().setFromObject(object);
  object.position.sub(box.getCenter(new THREE.Vector3()));
  const size = box.getSize(new THREE.Vector3()).length();
  if (size > 0) object.scale.setScalar(2.6 / size);
  return object;
}

function sceneSetup() {
  const scene = new THREE.Scene();
  scene.add(new THREE.HemisphereLight(0xffffff, 0x697870, 2.5));
  const key = new THREE.DirectionalLight(0xffffff, 3.2);
  key.position.set(4, 6, 5);
  scene.add(key);
  const fill = new THREE.DirectionalLight(0xffffff, 1.1);
  fill.position.set(-3, 1, -4);
  scene.add(fill);
  return scene;
}

export class ModelViewer {
  constructor(container) {
    this.container = container;
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    this.renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    container.append(this.renderer.domElement);
    this.scene = sceneSetup();
    this.camera = new THREE.PerspectiveCamera(35, 1, 0.01, 100);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = false;
    this.controls.minDistance = 1;
    this.controls.maxDistance = 15;
    this.controls.addEventListener('change', () => this.render());
    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(container);
    this.visible = true;
    this.reset();
    this.resize();
  }
  resize() {
    const { width, height } = this.container.getBoundingClientRect();
    if (!width || !height) return;
    this.camera.aspect = width / height;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(width, height);
    this.render();
  }
  reset() {
    this.camera.position.set(3.5, 2.5, 3.5);
    this.controls.target.set(0, 0, 0);
    this.controls.update();
    this.render();
  }
  render() {
    if (this.visible && !document.hidden) this.renderer.render(this.scene, this.camera);
  }
  setVisible(visible) {
    this.visible = visible;
    this.controls.enabled = visible;
    if (visible) this.resize();
  }
  async load(url, token) {
    this.abort?.abort();
    const controller = new AbortController();
    this.abort = controller;
    const object = await loadModel(url, token, controller.signal);
    if (controller.signal.aborted) { disposeObject(object); throw new DOMException('预览已取消', 'AbortError'); }
    this.clear(false);
    this.object = object;
    this.scene.add(object);
    this.reset();
  }
  clear(abort = true) {
    if (abort) this.abort?.abort();
    if (this.object) {
      this.scene.remove(this.object);
      disposeObject(this.object);
      this.object = null;
    }
    this.render();
  }
  dispose() {
    this.clear();
    this.resizeObserver.disconnect();
    this.controls.dispose();
    this.renderer.dispose();
    this.renderer.forceContextLoss();
    this.renderer.domElement.remove();
  }
}

// 全部卡片复用一个离屏上下文，渲染后立即释放模型几何体。
export class Thumbnails {
  constructor() {
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, preserveDrawingBuffer: true });
    this.renderer.setSize(300, 200);
    this.scene = sceneSetup();
    this.camera = new THREE.PerspectiveCamera(35, 1.5, 0.01, 100);
    this.camera.position.set(3.5, 2.5, 3.5);
    this.camera.lookAt(0, 0, 0);
    this.epoch = 0;
  }
  cancel() { this.epoch++; this.abort?.abort(); }
  async fill(items, token) {
    this.cancel();
    const epoch = this.epoch;
    for (const { img, url } of items) {
      if (epoch !== this.epoch) break;
      this.abort = new AbortController();
      let object;
      try {
        object = await loadModel(url, token, this.abort.signal);
        if (epoch !== this.epoch || !img.isConnected) continue;
        this.scene.add(object);
        this.renderer.render(this.scene, this.camera);
        img.src = this.renderer.domElement.toDataURL('image/png');
        img.classList.add('loaded');
      } catch (error) {
        if (error.name !== 'AbortError') img.alt = '预览暂不可用';
      } finally {
        if (object) { this.scene.remove(object); disposeObject(object); }
      }
    }
  }
  dispose() {
    this.cancel();
    this.renderer.dispose();
    this.renderer.forceContextLoss();
  }
}
