// Modified for proxrs: bounded consumption-driven direct-tcpip receive windows.
// See PROXRS_PATCH.md for provenance, behavior, and verification.

use std::sync::Arc;
use std::sync::atomic::Ordering;
use tokio::sync::mpsc::{Sender, error::SendError};

use super::{WindowSizeRef, ReceiveCredit};
use crate::ChannelMsg;

/// A handle to the [`super::Channel`]'s to be able to transmit messages
/// to it and update it's `window_size`.
#[derive(Debug)]
pub struct ChannelRef {
    pub(super) sender: Sender<ChannelMsg>,
    pub(super) window_size: WindowSizeRef,
    pub(crate) receive_credit: Option<Arc<ReceiveCredit>>,
}

impl ChannelRef {
    pub fn new(sender: Sender<ChannelMsg>) -> Self {
        Self {
            sender,
            window_size: WindowSizeRef::new(0),
            receive_credit: None,
        }
    }

    pub(crate) async fn send(&self, message: ChannelMsg) -> Result<(), SendError<ChannelMsg>> {
        let Some(credit) = &self.receive_credit else {
            return self.sender.send(message).await;
        };
        let lifecycle_bit = match &message {
            ChannelMsg::Data { data } | ChannelMsg::ExtendedData { data, .. } => {
                // Empty packets consume no SSH window and must consume no queue slot.
                if data.is_empty() { return Ok(()); }
                0
            }
            ChannelMsg::Open { .. } | ChannelMsg::OpenFailure(_) => 1,
            ChannelMsg::Eof => 2,
            ChannelMsg::Close => 4,
            // direct-tcpip is a byte stream. Window updates already reach the
            // write half separately; session-channel notifications do not apply.
            _ => return Ok(()),
        };
        if lifecycle_bit != 0
            && credit.control_seen.fetch_or(lifecycle_bit, Ordering::AcqRel) & lifecycle_bit != 0
        {
            return Ok(());
        }
        // Capacity is window bytes + four lifecycle slots. Every queued data
        // message costs >=1 byte of credit; no healthy peer can fill this queue.
        // Fail closed if the invariant is broken, never block the shared reader.
        self.sender.try_send(message).map_err(|error| SendError(error.into_inner()))
    }

    pub(crate) fn window_size(&self) -> &WindowSizeRef {
        &self.window_size
    }
}

impl std::ops::Deref for ChannelRef {
    type Target = Sender<ChannelMsg>;

    fn deref(&self) -> &Self::Target {
        &self.sender
    }
}

#[cfg(test)]
mod backpressure_tests {
    use super::*;
    use bytes::Bytes;
    use tokio::io::AsyncReadExt;
    use tokio::sync::{Notify, mpsc};
    use crate::channels::ChannelReadHalf;

    fn pair(window: usize) -> (ChannelRef, ChannelReadHalf, Arc<ReceiveCredit>) {
        let (sender, receiver) = mpsc::channel(window + 4);
        let credit = ReceiveCredit::new(Arc::new(Notify::new()));
        let mut reference = ChannelRef::new(sender);
        reference.receive_credit = Some(credit.clone());
        (reference, ChannelReadHalf { receiver, receive_credit: Some(credit.clone()) }, credit)
    }

    #[tokio::test]
    async fn byte_sized_packets_and_controls_cannot_block_shared_delivery() {
        let (reference, mut receiver, credit) = pair(8);
        for _ in 0..1000 {
            reference.send(ChannelMsg::Data { data: Bytes::new() }).await.unwrap();
            reference.send(ChannelMsg::WindowAdjusted { new_size: 10 }).await.unwrap();
        }
        for byte in 0..8 {
            reference.send(ChannelMsg::Data { data: Bytes::from(vec![byte]) }).await.unwrap();
        }
        for _ in 0..1000 {
            reference.send(ChannelMsg::Eof).await.unwrap();
            reference.send(ChannelMsg::Close).await.unwrap();
        }
        assert_eq!(credit.consumed.load(Ordering::Acquire), 0);
        assert_eq!(receiver.receiver.len(), 10);
        for _ in 0..8 { assert!(matches!(receiver.wait().await, Some(ChannelMsg::Data { .. }))); }
        assert_eq!(credit.consumed.load(Ordering::Acquire), 8);
        assert!(matches!(receiver.wait().await, Some(ChannelMsg::Eof)));
        assert!(matches!(receiver.wait().await, Some(ChannelMsg::Close)));
    }

    #[tokio::test]
    async fn partial_stream_reads_and_dropped_reader_return_exact_credit() {
        let (reference, mut receiver, credit) = pair(8);
        reference.send(ChannelMsg::Data { data: Bytes::from_static(b"12345678") }).await.unwrap();
        let mut reader = receiver.make_reader();
        let mut output = [0; 3];
        reader.read_exact(&mut output).await.unwrap();
        assert_eq!(&output, b"123");
        assert_eq!(credit.consumed.load(Ordering::Acquire), 3);
        drop(reader);
        assert_eq!(credit.consumed.load(Ordering::Acquire), 8);
        // A zero-window channel is woken by consumed bytes, even with no network input.
        tokio::time::timeout(std::time::Duration::from_millis(100), credit.notify.notified()).await.unwrap();
    }

    #[tokio::test]
    async fn extended_data_and_ignored_stream_credit_share_the_window() {
        let (reference, mut receiver, credit) = pair(8);
        reference.send(ChannelMsg::ExtendedData { data: Bytes::from_static(b"err"), ext: 1 }).await.unwrap();
        reference.send(ChannelMsg::Data { data: Bytes::from_static(b"ok") }).await.unwrap();
        let mut reader = receiver.make_reader();
        let mut output = [0; 2];
        reader.read_exact(&mut output).await.unwrap();
        assert_eq!(&output, b"ok");
        assert_eq!(credit.consumed.load(Ordering::Acquire), 5);
    }

    #[tokio::test]
    async fn invariant_breach_fails_immediately_instead_of_waiting() {
        let (reference, _receiver, _credit) = pair(1);
        for _ in 0..5 {
            reference.send(ChannelMsg::Data { data: Bytes::from_static(b"x") }).await.unwrap();
        }
        assert!(reference.send(ChannelMsg::Data { data: Bytes::from_static(b"x") }).await.is_err());
    }
}
