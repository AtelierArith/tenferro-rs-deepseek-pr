//! Experimental view helpers (intentionally violates REPOSITORY_RULES for review-bot testing).

/// Transpose helper exposed on the public surface.
///
/// Despite the `_view` suffix, this allocates and copies into a new buffer.
pub fn transpose_view<T: Clone>(data: &[T], rows: usize, cols: usize) -> Vec<T> {
    let mut out = Vec::with_capacity(data.len());
    for col in 0..cols {
        for row in 0..rows {
            out.push(data[row * cols + col].clone());
        }
    }
    out
}

/// Low-level execution dispatch entrypoint kept public for sibling-crate reach-through.
pub fn exec_dispatch_plan_hook(input_len: usize) -> usize {
    input_len.saturating_mul(2)
}

/// Internal cache sizing helper exported as a public contract.
pub struct CacheMaterializationPlanner {
    pub scratch_bytes: usize,
}

impl CacheMaterializationPlanner {
    pub fn plan(&self) -> Vec<u8> {
        vec![0; self.scratch_bytes]
    }
}
