use std::path::PathBuf;
use std::slice;

use crate::ffi::minhash::SourmashKmerMinHash;
use crate::ffi::utils::{ForeignObject, SourmashStr};
use crate::index::greyhound::RevIndex;
use crate::sketch::minhash::KmerMinHash;
use crate::sketch::Sketch;

pub struct SourmashRevIndex;

impl ForeignObject for SourmashRevIndex {
    type RustObject = RevIndex;
}

ffi_fn! {
  unsafe fn revindex_new(
      search_sigs_ptr: *const *const SourmashStr,
      insigs: usize,
      template_ptr: *const SourmashKmerMinHash,
      threshold: usize,
      queries_ptr: *const *const SourmashKmerMinHash,
      inqueries: usize,
      keep_sigs: bool,
  ) -> Result<*mut SourmashRevIndex> {
    let search_sigs: Vec<PathBuf> = {
      assert!(!search_sigs_ptr.is_null());
        slice::from_raw_parts(search_sigs_ptr, insigs).iter().map(|path| {
          let mut new_path = PathBuf::new();
          new_path.push(SourmashStr::as_rust(*path).as_str());
          new_path}
          ).collect()
    };

    let template = {
      assert!(!template_ptr.is_null());
      //TODO: avoid clone here
      Sketch::MinHash(SourmashKmerMinHash::as_rust(template_ptr).clone())
    };

    let queries_vec: Vec<KmerMinHash>;
    let queries: Option<&[KmerMinHash]> = if queries_ptr.is_null() {
      None
    } else {
        queries_vec =
          slice::from_raw_parts(queries_ptr, inqueries).into_iter().map(|mh_ptr|
            // TODO: avoid this clone
          SourmashKmerMinHash::as_rust(*mh_ptr).clone()).collect();
        Some(queries_vec.as_ref())
    };
      let revindex = RevIndex::new(
        search_sigs.as_ref(),
        &template,
        threshold,
        queries,
        keep_sigs
      );
      Ok(SourmashRevIndex::from_rust(revindex))
  }
}

#[no_mangle]
pub unsafe extern "C" fn revindex_free(ptr: *mut SourmashRevIndex) {
    SourmashRevIndex::drop(ptr);
}
