const DB_NAME = 'llmpod:image-cache'
const DB_VERSION = 1
const STORE_NAME = 'images'

/** IndexedDB 中的图片缓存记录。 */
export interface CachedImageRecord {
  blob: Blob
  /** 写入时间戳,预留后续 TTL 扩展 */
  updatedAt: number
}

let dbPromise: Promise<IDBDatabase | null> | null = null

function openDB(): Promise<IDBDatabase | null> {
  if (!dbPromise) {
    dbPromise = new Promise((resolve) => {
      if (typeof indexedDB === 'undefined') {
        resolve(null)
        return
      }
      try {
        const request = indexedDB.open(DB_NAME, DB_VERSION)
        request.onupgradeneeded = () => {
          request.result.createObjectStore(STORE_NAME)
        }
        request.onsuccess = () => resolve(request.result)
        request.onerror = () => resolve(null)
        request.onblocked = () => resolve(null)
      }
      catch {
        resolve(null)
      }
    })
  }
  return dbPromise
}

/** 读取缓存记录;IndexedDB 不可用或读取失败时返回 undefined。 */
export async function getCachedRecord(url: string): Promise<CachedImageRecord | undefined> {
  const db = await openDB()
  if (!db) return undefined
  return new Promise((resolve) => {
    try {
      const request = db.transaction(STORE_NAME, 'readonly').objectStore(STORE_NAME).get(url)
      request.onsuccess = () => resolve(request.result as CachedImageRecord | undefined)
      request.onerror = () => resolve(undefined)
    }
    catch {
      resolve(undefined)
    }
  })
}

/** 写入缓存记录;写入失败静默忽略(缓存失败不影响主流程)。 */
export async function putCachedRecord(url: string, blob: Blob): Promise<void> {
  const db = await openDB()
  if (!db) return
  return new Promise((resolve) => {
    try {
      const tx = db.transaction(STORE_NAME, 'readwrite')
      const record: CachedImageRecord = { blob, updatedAt: Date.now() }
      tx.objectStore(STORE_NAME).put(record, url)
      tx.oncomplete = () => resolve()
      tx.onerror = () => resolve()
      tx.onabort = () => resolve()
    }
    catch {
      resolve()
    }
  })
}

/** 删除指定 url 的缓存记录。 */
export async function removeCachedRecord(url: string): Promise<void> {
  const db = await openDB()
  if (!db) return
  return new Promise((resolve) => {
    try {
      const tx = db.transaction(STORE_NAME, 'readwrite')
      tx.objectStore(STORE_NAME).delete(url)
      tx.oncomplete = () => resolve()
      tx.onerror = () => resolve()
      tx.onabort = () => resolve()
    }
    catch {
      resolve()
    }
  })
}

/** 清空全部缓存记录。 */
export async function clearCachedRecords(): Promise<void> {
  const db = await openDB()
  if (!db) return
  return new Promise((resolve) => {
    try {
      const tx = db.transaction(STORE_NAME, 'readwrite')
      tx.objectStore(STORE_NAME).clear()
      tx.oncomplete = () => resolve()
      tx.onerror = () => resolve()
      tx.onabort = () => resolve()
    }
    catch {
      resolve()
    }
  })
}
